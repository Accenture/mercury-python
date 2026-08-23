"""
The primitive in-process event bus - the single dispatch pipeline.

Every invocation reaches a function the same way: through a per-route FIFO
mailbox consumed by ``instances`` worker tasks (the engines' semantics - the
parameter is faithful). The HTTP host and the local side of PostOffice are
thin ingress adapters over this bus; neither has its own invocation path.

Deliberately primitive, riding asyncio's native machinery:

- Two operations only: :meth:`EventBus.deliver` (RPC - enqueue with a reply
  future, bounded by the caller's ttl) and :meth:`EventBus.publish`
  (drop-n-forget - enqueue and return the 202-shape acknowledgement).
- **No spill tier and no queue cap**: back-pressure belongs to the tier that
  owns recovery - the engines' flows and graphs. A leaf host fails fast by
  deadline (the 408 envelope) instead of hoarding work, and a queued RPC
  delivery whose caller already timed out is skipped (dead-work check).
- In-memory only: in-flight events die with the process, exactly like the
  engines' own in-memory bus; at-least-once comes from flow-level retries.
- No orchestration, no flows, no persistence, no pub/sub broadcast.

The bus is internal: application code uses ``@preload`` and ``PostOffice``,
never this module - the same way engine developers never touch the engine bus.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .envelope import EventEnvelope, iso_utc
from .exceptions import AppException
from .log import get_logger
from .trace import TraceInfo, _reset_trace, _set_trace

if TYPE_CHECKING:
    from .registry import ServiceDef

log = get_logger("mercury.bus")


class DeliveryTimeout(Exception):
    """An RPC delivery missed its deadline; adapters shape the 408 for their protocol."""

    def __init__(self, ttl_ms: int):
        super().__init__(f"Timeout for {ttl_ms} ms")
        self.ttl_ms = ttl_ms


def async_ack() -> EventEnvelope:
    """The 202 drop-n-forget acknowledgement (EventApiService shape)."""
    return EventEnvelope().set_status(202).set_body(
        {"type": "async", "delivered": True, "time": iso_utc()})


@dataclass
class _Delivery:
    service: ServiceDef
    headers: dict[str, str]
    body: Any
    trace_id: str | None
    trace_path: str | None
    cid: str | None
    reply: asyncio.Future[EventEnvelope] | None  # None = drop-n-forget


class EventBus:
    """Per-registry bus: one FIFO mailbox and N workers per registered route."""

    def __init__(self) -> None:
        self._mailboxes: dict[str, asyncio.Queue[_Delivery]] = {}
        self._workers: dict[str, list[asyncio.Task[None]]] = {}

    def _mailbox(self, service: ServiceDef) -> asyncio.Queue[_Delivery]:
        mailbox = self._mailboxes.get(service.route)
        if mailbox is None:
            # lazy: mailbox and workers bind to the running event loop on first use
            mailbox = asyncio.Queue()
            self._mailboxes[service.route] = mailbox
            self._workers[service.route] = [
                asyncio.get_running_loop().create_task(
                    self._run_worker(mailbox), name=f"mercury-bus-{service.route}-{n}")
                for n in range(service.instances)
            ]
        return mailbox

    async def deliver(self, service: ServiceDef, headers: dict[str, str], body: Any,
                      ttl_ms: int, *, trace_id: str | None = None,
                      trace_path: str | None = None, cid: str | None = None) -> EventEnvelope:
        """RPC: enqueue and await the reply envelope within the ttl."""
        reply: asyncio.Future[EventEnvelope] = asyncio.get_running_loop().create_future()
        self._mailbox(service).put_nowait(_Delivery(
            service=service, headers=headers, body=body,
            trace_id=trace_id, trace_path=trace_path, cid=cid, reply=reply))
        try:
            return await asyncio.wait_for(reply, timeout=max(100, ttl_ms) / 1000)
        except asyncio.TimeoutError:
            raise DeliveryTimeout(ttl_ms) from None

    def publish(self, service: ServiceDef, headers: dict[str, str], body: Any, *,
                trace_id: str | None = None, trace_path: str | None = None,
                cid: str | None = None) -> EventEnvelope:
        """Drop-n-forget: enqueue and return the 202-shape acknowledgement."""
        self._mailbox(service).put_nowait(_Delivery(
            service=service, headers=headers, body=body,
            trace_id=trace_id, trace_path=trace_path, cid=cid, reply=None))
        return async_ack()

    async def close(self) -> None:
        """Cancel all workers (tests and orderly shutdown)."""
        for workers in self._workers.values():
            for worker in workers:
                worker.cancel()
        for workers in self._workers.values():
            for worker in workers:
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
        self._workers.clear()
        self._mailboxes.clear()

    async def _run_worker(self, mailbox: asyncio.Queue[_Delivery]) -> None:
        while True:
            delivery = await mailbox.get()
            # dead-work check: the caller of a queued RPC already gave up (408 sent) -
            # skip instead of computing a reply nobody reads
            if delivery.reply is not None and delivery.reply.done():
                continue
            reply = await self._execute(delivery)
            if delivery.reply is not None:
                if not delivery.reply.done():
                    delivery.reply.set_result(reply)
            elif reply.has_error():
                log.warning("Async event %s ended with status %d - %s",
                            delivery.service.route, reply.get_status(), reply.body)

    async def _execute(self, delivery: _Delivery) -> EventEnvelope:
        """Run the handler under its trace context and shape the outcome as a reply."""
        service = delivery.service
        info = TraceInfo(trace_id=delivery.trace_id, trace_path=delivery.trace_path,
                         cid=delivery.cid)
        token = _set_trace(info)
        start = time.perf_counter()
        # noinspection PyBroadException
        try:
            if service.is_async:
                result = await service.handler(delivery.headers, delivery.body)
            else:
                # copy_context() carries the trace contextvar into the executor thread
                context = contextvars.copy_context()
                result = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: context.run(service.handler, delivery.headers,
                                              delivery.body))
            reply = result if isinstance(result, EventEnvelope) else EventEnvelope(body=result)
        except AppException as e:
            reply = EventEnvelope().set_status(e.status).set_body(e.message)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # any handler failure becomes the portable error contract
            # (status 500 + message + stack), mirroring the engines
            reply = EventEnvelope().set_status(500).set_body(str(e))
            reply.stack = traceback.format_exc(limit=20)
        finally:
            _reset_trace(token)
        reply.sender = reply.sender or service.route
        reply.exec_time = round((time.perf_counter() - start) * 1000, 3)
        if info.annotations:
            reply.annotations.update(info.annotations)
        return reply
