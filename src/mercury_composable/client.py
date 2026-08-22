"""
Thin Event-over-HTTP client — the PostOffice analog.

Sends an event envelope to a peer's ``/api/event`` endpoint (a Java or Rust
engine application, or another polyglot function host) with the same HTTP
contract as the engines' relay: ``content-type: application/octet-stream``,
``accept: */*``, ``x-no-stream: true``, ``x-ttl`` (ms), ``x-async: true`` for
drop-n-forget, optional security headers, and trace headers
(``X-Trace-Id`` plus a W3C ``traceparent`` when the trace id is W3C-shaped).

The decoded reply envelope is authoritative: an error from the target rides
back as a normal envelope with status >= 400 — inspect ``reply.get_status()``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

import aiohttp

from .envelope import EventEnvelope
from .exceptions import AppException
from .trace import get_trace

_W3C_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_W3C_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")


class PostOffice:
    """Event-over-HTTP client for calling functions on peer applications."""

    def __init__(self, endpoint: Optional[str] = None,
                 security_headers: Optional[Dict[str, str]] = None):
        self.endpoint = endpoint
        self.security_headers = dict(security_headers or {})
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> "PostOffice":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    def _http_headers(self, timeout_ms: int, is_async: bool,
                      event: EventEnvelope) -> Dict[str, str]:
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

    def _build_event(self, route: str, body: Any, headers: Optional[Dict[str, str]],
                     from_route: Optional[str], cid: Optional[str]) -> EventEnvelope:
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

    async def _call(self, route: str, body: Any, headers: Optional[Dict[str, str]],
                    timeout_ms: int, endpoint: Optional[str], is_async: bool,
                    from_route: Optional[str], cid: Optional[str]) -> EventEnvelope:
        url = endpoint or self.endpoint
        if not url:
            raise ValueError("Missing event endpoint - "
                             "e.g. PostOffice(endpoint='http://peer:8085/api/event')")
        event = self._build_event(route, body, headers, from_route, cid)
        session = await self._get_session()
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
                      headers: Optional[Dict[str, str]] = None,
                      timeout_ms: int = 30000,
                      endpoint: Optional[str] = None,
                      from_route: Optional[str] = None,
                      cid: Optional[str] = None) -> EventEnvelope:
        """RPC call: returns the target function's reply envelope."""
        return await self._call(route, body, headers, timeout_ms, endpoint,
                                False, from_route, cid)

    async def send(self, route: str, body: Any = None, *,
                   headers: Optional[Dict[str, str]] = None,
                   timeout_ms: int = 30000,
                   endpoint: Optional[str] = None,
                   from_route: Optional[str] = None,
                   cid: Optional[str] = None) -> EventEnvelope:
        """Drop-n-forget: returns the peer's 202 delivery acknowledgement envelope."""
        return await self._call(route, body, headers, timeout_ms, endpoint,
                                True, from_route, cid)
