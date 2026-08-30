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
import secrets
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from .envelope import EventEnvelope, iso_utc
from .exceptions import AppException
from .log import get_logger
from .trace import (
    MY_CID_TAG,
    MY_CORRELATION_ID,
    RPC_TAG,
    TraceInfo,
    _reset_trace,
    _set_trace,
)

if TYPE_CHECKING:
    from .registry import Handler, InterceptorHandler, ServiceDef

log = get_logger("mercury.bus")
# the engines' distributed-trace log stream (Java Telemetry parity)
telemetry_log = get_logger("distributed.tracing")

# The loop hosting the current sync handler - stamped by _execute before the
# handler is dispatched to the executor thread (copy_context carries it), so
# PostOffice's sync bridge can submit coroutines back to the right loop.
_HOST_LOOP: contextvars.ContextVar[asyncio.AbstractEventLoop | None] = \
    contextvars.ContextVar("mercury_host_loop", default=None)


def get_host_loop() -> asyncio.AbstractEventLoop | None:
    """The event loop hosting the current sync handler (None off-host)."""
    return _HOST_LOOP.get()


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
    reply: asyncio.Future[EventEnvelope] | None  # drop-n-forget deliveries carry no reply future
    # the raw envelope, for interceptor handlers (they receive it verbatim -
    # reply_to and correlation id travel the engines' way)
    envelope: EventEnvelope | None = None


def _business_cid(delivery: _Delivery) -> str | None:
    """The caller's business correlation-id at delivery: the engine-managed
    my_cid envelope tag, else a my_correlation_id view already injected by an
    HTTP host (the engines' WorkerHandler resolution order)."""
    tag = delivery.envelope.tags.get(MY_CID_TAG) if delivery.envelope else None
    return tag or delivery.headers.get(MY_CORRELATION_ID)


def _headers_view(delivery: _Delivery, my_cid: str | None) -> dict[str, str]:
    """The handler's header view, with the read-only business correlation-id
    injected at delivery (engine parity)."""
    if my_cid and MY_CORRELATION_ID not in delivery.headers:
        return {**delivery.headers, MY_CORRELATION_ID: my_cid}
    return delivery.headers


def _trace_info(delivery: _Delivery, my_cid: str | None) -> TraceInfo:
    """The execution's trace context. Under a trace, every execution mints its
    own 16-hex span and records the caller's span (from the inbound envelope)
    as its parent - the engines' WorkerHandler model."""
    span_id = secrets.token_hex(8) if delivery.trace_id else None
    parent = delivery.envelope.span_id if delivery.envelope else None
    return TraceInfo(route=delivery.service.route,
                     trace_id=delivery.trace_id, trace_path=delivery.trace_path,
                     cid=delivery.cid, my_correlation_id=my_cid,
                     span_id=span_id, parent_span_id=parent)


def _is_rpc(delivery: _Delivery) -> bool:
    """True for an RPC round-trip: a local reply future, or the engines' rpc
    envelope tag transported over the wire. RPC legs emit no trace dataset
    (engine parity) - their metrics fold into the caller's view."""
    if delivery.reply is not None:
        return True
    return bool(delivery.envelope and delivery.envelope.tags.get(RPC_TAG))


def _emit_trace(delivery: _Delivery, info: TraceInfo, start: str,
                exec_time: float, status: int, success: bool,
                exception: str | None) -> None:
    """Emit the engines' distributed-trace dataset for a traced, non-RPC
    execution - the same record shape the Java reference engine logs
    (message = {"trace": {...}, "annotations": {...}}), so polyglot log
    aggregation stitches spans across all runtimes."""
    if not info.trace_id or _is_rpc(delivery):
        return
    from .actuator import app_origin  # late: actuator imports the registry chain
    trace: dict[str, Any] = {
        "origin": app_origin(), "id": info.trace_id, "path": info.trace_path,
        "service": delivery.service.route, "start": start, "success": success,
        "from": (delivery.envelope.sender if delivery.envelope else None) or "unknown",
        "exec_time": exec_time, "status": status,
    }
    if not success and exception:
        trace["exception"] = exception
    if info.span_id:
        trace["span_id"] = info.span_id
    if info.parent_span_id:
        trace["parent_span_id"] = info.parent_span_id
    dataset: dict[str, Any] = {"trace": trace}
    if info.annotations:
        dataset["annotations"] = dict(info.annotations)
    telemetry_log.info(dataset)


class EventBus:
    """Per-registry bus: one FIFO mailbox and N workers per registered route."""

    def __init__(self) -> None:
        self._mailboxes: dict[str, asyncio.Queue[_Delivery]] = {}
        self._workers: dict[str, list[asyncio.Task[None]]] = {}
        # per-request reply sinks (the engines' inbox idea): generated local
        # route names backed by queues - the reply_to addressing of interceptor
        # dispatch and of streaming responses. Local-only by design.
        self._sinks: dict[str, asyncio.Queue[EventEnvelope]] = {}
        # backref for reply routing (registry constructs and owns this bus)
        self._registry: Any = None

    def bind_registry(self, registry: Any) -> None:
        self._registry = registry

    def open_sink(self) -> tuple[str, asyncio.Queue[EventEnvelope]]:
        """Open a per-request reply sink under a generated local route name."""
        route = f"inbox.{uuid.uuid4().hex}"
        queue: asyncio.Queue[EventEnvelope] = asyncio.Queue()
        self._sinks[route] = queue
        return route, queue

    def close_sink(self, route: str) -> None:
        self._sinks.pop(route, None)

    def offer_sink(self, route: str, event: EventEnvelope) -> bool:
        """Deliver an envelope to a reply sink; False when the sink is gone
        (a completed, timed-out or disconnected request) - late segments are
        no-op drops, the engines' semantics."""
        queue = self._sinks.get(route)
        if queue is None:
            return False
        queue.put_nowait(event)
        return True

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
                      trace_path: str | None = None, cid: str | None = None,
                      envelope: EventEnvelope | None = None) -> EventEnvelope:
        """RPC: enqueue and await the reply envelope within the ttl.

        ``envelope`` is delivery context only (engine-managed tags such as the
        business correlation-id); the handler still receives headers + body.
        """
        reply: asyncio.Future[EventEnvelope] = asyncio.get_running_loop().create_future()
        self._mailbox(service).put_nowait(_Delivery(
            service=service, headers=headers, body=body,
            trace_id=trace_id, trace_path=trace_path, cid=cid, reply=reply,
            envelope=envelope))
        try:
            return await asyncio.wait_for(reply, timeout=max(100, ttl_ms) / 1000)
        except asyncio.TimeoutError:
            raise DeliveryTimeout(ttl_ms) from None

    def publish(self, service: ServiceDef, headers: dict[str, str], body: Any, *,
                trace_id: str | None = None, trace_path: str | None = None,
                cid: str | None = None,
                envelope: EventEnvelope | None = None) -> EventEnvelope:
        """Drop-n-forget: enqueue and return the 202-shape acknowledgement."""
        self._mailbox(service).put_nowait(_Delivery(
            service=service, headers=headers, body=body,
            trace_id=trace_id, trace_path=trace_path, cid=cid, reply=None,
            envelope=envelope))
        return async_ack()

    def publish_envelope(self, service: ServiceDef, event: EventEnvelope) -> None:
        """Route one envelope to a local function (the reply_to mechanism):
        drop-n-forget delivery carrying the raw envelope, so an interceptor
        handler receives reply_to and the correlation id the engines' way."""
        self._mailbox(service).put_nowait(_Delivery(
            service=service, headers=dict(event.headers), body=event.body,
            trace_id=event.trace_id, trace_path=event.trace_path, cid=event.cid,
            reply=None, envelope=event))

    async def close(self) -> None:
        """Cancel all workers (tests and orderly shutdown)."""
        cancelled = [worker for workers in self._workers.values() for worker in workers]
        for worker in cancelled:
            worker.cancel()
        # return_exceptions collects the workers' own CancelledError outcomes;
        # a cancellation of close() itself still propagates from the gather
        await asyncio.gather(*cancelled, return_exceptions=True)
        self._workers.clear()
        self._mailboxes.clear()

    async def _run_worker(self, mailbox: asyncio.Queue[_Delivery]) -> None:
        # workers are long-lived tasks created lazily on first use, so they
        # inherit the creating task's contextvars - clear the trace so nothing
        # from an arbitrary first caller leaks into later executions' logs
        _set_trace(None)
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
        if delivery.service.interceptor:
            return await self._execute_interceptor(delivery)
        service = delivery.service
        my_cid = _business_cid(delivery)
        headers = _headers_view(delivery, my_cid)
        info = _trace_info(delivery, my_cid)
        token = _set_trace(info)
        start_iso = iso_utc()
        start = time.perf_counter()
        # noinspection PyBroadException
        try:
            handler = cast("Handler", service.handler)
            if service.is_async:
                result = await handler(headers, delivery.body)
            else:
                # stamp the host loop (for the PostOffice sync bridge), then let
                # copy_context() carry trace + loop into the executor thread
                loop = asyncio.get_running_loop()
                loop_token = _HOST_LOOP.set(loop)
                try:
                    context = contextvars.copy_context()
                finally:
                    _HOST_LOOP.reset(loop_token)
                result = await loop.run_in_executor(
                    None, lambda: context.run(handler, headers, delivery.body))
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
        _emit_trace(delivery, info, start_iso, reply.exec_time,
                    reply.get_status(), not reply.has_error(),
                    str(reply.body) if reply.has_error() else None)
        return reply

    async def _execute_interceptor(self, delivery: _Delivery) -> EventEnvelope:
        """Run an interceptor handler: it receives the raw envelope, replies
        manually through reply_to (the engines' @EventInterceptor contract),
        and its return value is discarded. An uncaught exception becomes an
        error envelope to the delivery's reply_to - so a caller waiting on a
        reply sink sees it - and a streaming host renders it in-band."""
        service = delivery.service
        event = delivery.envelope or EventEnvelope(
            to=service.route, body=delivery.body, headers=dict(delivery.headers))
        my_cid = _business_cid(delivery)
        headers = _headers_view(delivery, my_cid)
        info = _trace_info(delivery, my_cid)
        token = _set_trace(info)
        start_iso = iso_utc()
        start = time.perf_counter()
        error: Exception | None = None
        handler = cast("InterceptorHandler", service.handler)
        try:
            if service.is_async:
                await handler(headers, event)
            else:
                loop = asyncio.get_running_loop()
                loop_token = _HOST_LOOP.set(loop)
                try:
                    context = contextvars.copy_context()
                finally:
                    _HOST_LOOP.reset(loop_token)
                await loop.run_in_executor(
                    None, lambda: context.run(handler, headers, event))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            error = e
            self._reply_interceptor_error(service.route, event, e)
        finally:
            _reset_trace(token)
        status = error.status if isinstance(error, AppException) \
            else (500 if error else 200)
        _emit_trace(delivery, info, start_iso,
                    round((time.perf_counter() - start) * 1000, 3),
                    status, error is None, str(error) if error else None)
        # an interceptor's own outcome is never auto-replied
        return EventEnvelope()

    def _reply_interceptor_error(self, route: str, event: EventEnvelope,
                                 e: Exception) -> None:
        if isinstance(e, AppException):
            error = EventEnvelope().set_status(e.status).set_body(e.message)
        else:
            error = EventEnvelope().set_status(500).set_body(str(e))
            error.stack = traceback.format_exc(limit=20)
        error.sender = route
        if event.cid:
            error.set_correlation_id(event.cid)
        reply_to = event.reply_to
        delivered = bool(
            reply_to and self._registry is not None
            and self._registry.send_event(error.set_to(reply_to)))
        if not delivered:
            log.warning("Interceptor %s ended with status %d - %s",
                        route, error.get_status(), error.body)
