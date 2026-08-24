"""
The PostOffice — local and remote function calls with one envelope contract.

**Remote** (an ``endpoint`` is given, on the constructor or per call): sends
the event envelope to a peer's ``/api/event`` — a Java or Rust engine
application, or another polyglot function host — with the same HTTP contract
as the engines' relay: ``content-type: application/octet-stream``,
``accept: */*``, ``x-no-stream: true``, ``x-ttl`` (ms), ``x-async: true`` for
drop-n-forget, optional security headers, and trace headers (``X-Trace-Id``
plus a W3C ``traceparent`` when the trace id is W3C-shaped). The remote
target must be public (its host answers 403 for private routes).

**Local** (no endpoint): the call goes through this application's primitive
event bus to a locally registered function — private OR public, the engines'
semantics (``private`` means in-app only). Headers are delivered verbatim
(ingress hygiene applies to the wire, not to in-app calls), the ttl bounds
the wait with the standard 408 envelope, and the reply shape is identical to
the remote path. Local eventing is for simple leaf-side composition; workflow
processing belongs in Event Script and Knowledge Graph on the engines.

The decoded reply envelope is authoritative in both modes: an error rides
back as a normal envelope with status >= 400 — inspect ``reply.get_status()``.

**Sync bridge**: a plain ``def`` handler runs on an executor thread with no
event loop of its own, so it cannot ``await``. :meth:`PostOffice.request_sync`
and :meth:`PostOffice.send_sync` submit the same calls onto the host loop and
block only the handler's worker thread — never the event loop. The caller's
trace context rides across the bridge, so the trace chain is unbroken. Calling
the sync bridge from async code is refused with a teaching error (``await
request()`` is the async way).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
from collections.abc import Callable, Coroutine
from typing import Any

import aiohttp

from .bus import DeliveryTimeout, get_host_loop
from .envelope import EventEnvelope
from .exceptions import AppException
from .registry import FunctionRegistry, default_registry
from .trace import _reset_trace, _set_trace, get_trace

_W3C_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_W3C_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")


def _build_event(route: str, body: Any, headers: dict[str, str] | None,
                 from_route: str | None, cid: str | None) -> EventEnvelope:
    """Build the outbound envelope, inheriting the current trace context."""
    event = EventEnvelope(to=route, body=body, headers=headers or {})
    if from_route:
        event.set_from(from_route)
    info = get_trace()
    if info and info.trace_id:
        event.set_trace(info.trace_id, info.trace_path or route)
    effective_cid = cid or (info.cid if info else None)
    if effective_cid:
        event.set_correlation_id(effective_cid)
    return event


class PostOffice:
    """Event-over-HTTP client for calling functions on peer applications."""

    def __init__(self, endpoint: str | None = None,
                 security_headers: dict[str, str] | None = None,
                 registry: FunctionRegistry | None = None):
        self.endpoint = endpoint
        self.security_headers = dict(security_headers or {})
        self._registry = registry or default_registry
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        # called from the running event loop only (inside request/send)
        session = self._session
        if session is None or session.closed:
            session = aiohttp.ClientSession()
            self._session = session
        return session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # PYI034 wants '-> Self', which needs python >= 3.11; switch when the
    # floor moves past 3.10
    async def __aenter__(self) -> PostOffice:  # noqa: PYI034
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    def _http_headers(self, timeout_ms: int, is_async: bool,
                      event: EventEnvelope) -> dict[str, str]:
        headers = {
            "content-type": "application/octet-stream",
            "accept": "*/*",
            "x-no-stream": "true",
            "x-ttl": str(max(100, timeout_ms)),
        }
        if is_async:
            headers["x-async"] = "true"
        for key, value in self.security_headers.items():
            if key.lower() != "x-event-format":
                headers[key] = value
        if event.trace_id:
            headers["X-Trace-Id"] = event.trace_id
            if _W3C_TRACE_ID.fullmatch(event.trace_id) and event.span_id \
                    and _W3C_SPAN_ID.fullmatch(event.span_id):
                headers["traceparent"] = f"00-{event.trace_id}-{event.span_id}-01"
        return headers

    async def _call_local(self, route: str, body: Any, headers: dict[str, str] | None,
                          timeout_ms: int, is_async: bool, from_route: str | None,
                          cid: str | None) -> EventEnvelope:
        """In-app delivery through the primitive event bus (private OR public)."""
        service = self._registry.get(route)
        if service is None:
            return EventEnvelope().set_status(404).set_body(f"Route {route} not found")
        event = _build_event(route, body, headers, from_route, cid)
        bus = self._registry.bus
        if is_async:
            return bus.publish(service, event.headers, event.body, trace_id=event.trace_id,
                               trace_path=event.trace_path, cid=event.cid)
        try:
            return await bus.deliver(service, event.headers, event.body, timeout_ms,
                                     trace_id=event.trace_id, trace_path=event.trace_path,
                                     cid=event.cid)
        except DeliveryTimeout:
            return EventEnvelope().set_status(408).set_body(f"Timeout for {timeout_ms} ms")

    async def _call(self, route: str, body: Any, headers: dict[str, str] | None,
                    timeout_ms: int, endpoint: str | None, is_async: bool,
                    from_route: str | None, cid: str | None) -> EventEnvelope:
        url = endpoint or self.endpoint
        if not url:
            # no endpoint = local: the engines' semantics for an in-app po call
            return await self._call_local(route, body, headers, timeout_ms,
                                          is_async, from_route, cid)
        event = _build_event(route, body, headers, from_route, cid)
        session = self._get_session()
        # +100 ms cushion so the HTTP client does not time out before the target
        client_timeout = aiohttp.ClientTimeout(total=(max(100, timeout_ms) + 100) / 1000)
        async with session.post(url, data=event.to_bytes(),
                                headers=self._http_headers(timeout_ms, is_async, event),
                                timeout=client_timeout) as response:
            payload = await response.read()
            try:
                return EventEnvelope.from_bytes(payload)
            except ValueError as e:
                raise AppException(response.status,
                                   f"Invalid event-over-http response - {e}") from e

    async def request(self, route: str, body: Any = None, *,
                      headers: dict[str, str] | None = None,
                      timeout_ms: int = 30000,
                      endpoint: str | None = None,
                      from_route: str | None = None,
                      cid: str | None = None) -> EventEnvelope:
        """RPC call: returns the target function's reply envelope."""
        return await self._call(route, body, headers, timeout_ms, endpoint,
                                False, from_route, cid)

    async def send(self, route: str, body: Any = None, *,
                   headers: dict[str, str] | None = None,
                   timeout_ms: int = 30000,
                   endpoint: str | None = None,
                   from_route: str | None = None,
                   cid: str | None = None) -> EventEnvelope:
        """Drop-n-forget: returns the peer's 202 delivery acknowledgement envelope."""
        return await self._call(route, body, headers, timeout_ms, endpoint,
                                True, from_route, cid)

    @staticmethod
    def _run_sync(factory: Callable[[], Coroutine[Any, Any, EventEnvelope]],
                  timeout_ms: int) -> EventEnvelope:
        """Run a PostOffice coroutine from a sync handler's thread on the host loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # no loop in this thread - the sync bridge is applicable
        else:
            raise RuntimeError(
                "request_sync()/send_sync() must not be called on the event loop - "
                "await request()/send() instead")
        loop = get_host_loop()
        if loop is None:
            raise RuntimeError(
                "No Mercury host event loop in context - the sync bridge works inside "
                "a hosted sync function; elsewhere use asyncio.run(po.request(...))")
        info = get_trace()  # carried into this thread by the bus dispatch

        async def bridged() -> EventEnvelope:
            if info is None:
                return await factory()
            # a submitted Task runs in its own context - re-establish the caller's
            # trace there (the SAME TraceInfo object, so the chain is unbroken)
            token = _set_trace(info)
            try:
                return await factory()
            finally:
                _reset_trace(token)

        future = asyncio.run_coroutine_threadsafe(bridged(), loop)
        try:
            # inner deadlines already shape 408 envelopes; this outer margin only
            # guards a wedged path so an executor thread can never hang forever
            return future.result(timeout=max(100, timeout_ms) / 1000 + 10)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return EventEnvelope().set_status(408).set_body(f"Timeout for {timeout_ms} ms")

    def request_sync(self, route: str, body: Any = None, *,
                     headers: dict[str, str] | None = None,
                     timeout_ms: int = 30000,
                     endpoint: str | None = None,
                     from_route: str | None = None,
                     cid: str | None = None) -> EventEnvelope:
        """RPC from a plain ``def`` handler: blocks this worker thread only,
        never the event loop, while the call runs on the host loop."""
        return self._run_sync(
            lambda: self.request(route, body, headers=headers, timeout_ms=timeout_ms,
                                 endpoint=endpoint, from_route=from_route, cid=cid),
            timeout_ms)

    def send_sync(self, route: str, body: Any = None, *,
                  headers: dict[str, str] | None = None,
                  timeout_ms: int = 30000,
                  endpoint: str | None = None,
                  from_route: str | None = None,
                  cid: str | None = None) -> EventEnvelope:
        """Drop-n-forget from a plain ``def`` handler (see :meth:`request_sync`)."""
        return self._run_sync(
            lambda: self.send(route, body, headers=headers, timeout_ms=timeout_ms,
                              endpoint=endpoint, from_route=from_route, cid=cid),
            timeout_ms)
