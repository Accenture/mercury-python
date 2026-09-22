"""
The OTLP/HTTP export: one request per span through aiohttp (the host's own
HTTP client), with the engines' retry policy and failure diagnostics (the Rust
port's ``export.rs``, the Java ``OtelForwarderContext``).

**Retry.** Telemetry delivery is at-least-once by design - duplicates are
tolerated, drops are what hurt - so a transport failure (connect refused, TLS,
a killed keep-alive, a timeout) and the retryable HTTP statuses (408, 429,
502, 503, 504) are retried on the OpenTelemetry SDK's default bounded backoff:
5 attempts, 1 s growing by 1.5x. Any other status fails at once - a 401 will
not get better by waiting.

**Diagnostics.** A rejected export is actionable from the forwarder's own
warning line: the status leads, the backend's response body follows
(whitespace-collapsed, bounded), and the rejections that actually happen get a
hint. Request headers are never rendered, so no credential can reach the log.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp

from ..log import get_logger
from . import otlp
from .config import ENDPOINT, ForwarderSettings
from .span import Span

log = get_logger("mercury.otel")

INSTRUMENTATION_SCOPE = "mercury-composable-python"
# statuses worth another attempt (the OpenTelemetry SDK's set, plus 408)
RETRYABLE_STATUSES = (408, 429, 502, 503, 504)
# the waits between attempts: 5 attempts, 1 s x 1.5^n
DEFAULT_BACKOFF_MS = (1000, 1500, 2250, 3375)
CONTENT_TYPE = "application/x-protobuf"
MAX_BODY_CHARS = 256


@dataclass
class ExportFailure(Exception):
    """Why an export gave up: the last attempt's diagnostic and the attempt count."""
    attempts: int
    detail: str

    def __str__(self) -> str:
        if self.attempts > 1:
            return f"{self.detail} (after {self.attempts} attempts)"
        return self.detail


class _AttemptError(Exception):
    def __init__(self, retryable: bool, detail: str) -> None:
        super().__init__(detail)
        self.retryable = retryable
        self.detail = detail


def validate_endpoint(url: str) -> str:
    """The endpoint must be an http(s) URL with a host - checked at start-up so
    a misconfiguration surfaces before the first span."""
    trimmed = url.strip()
    parts = urlsplit(trimmed)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ValueError(f"{ENDPOINT}='{url}' must be an http(s) URL including the signal path, "
                         f"e.g. http://localhost:4318/v1/traces")
    return trimmed


class Exporter:
    """The OTLP/HTTP exporter for one endpoint."""

    def __init__(self, settings: ForwarderSettings,
                 backoff_ms: tuple[int, ...] = DEFAULT_BACKOFF_MS) -> None:
        self.endpoint = validate_endpoint(settings.endpoint)
        self.service_name = settings.service_name
        self.scope_version = settings.scope_version
        self.compression = settings.compression
        self.timeout_ms = settings.timeout_ms
        self.connect_timeout_ms = settings.connect_timeout_ms
        self._headers = settings.headers
        self._backoff = [ms / 1000 for ms in backoff_ms]
        self._session: aiohttp.ClientSession | None = None

    def header_names(self) -> list[str]:
        """The names of the request headers that resolve right now (values are
        never exposed - this feeds the start-up line)."""
        return [k for k, _ in self._headers()]

    def encode(self, span: Span) -> bytes:
        """The OTLP request body for one span."""
        return otlp.encode_export_request(self.service_name, INSTRUMENTATION_SCOPE,
                                          self.scope_version, span)

    async def export(self, span: Span) -> None:
        """Export one span, retrying transient failures on the backoff schedule;
        raises :class:`ExportFailure` when the attempts are exhausted or the
        backend's answer is final."""
        body = self.encode(span)
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._attempt(body)
                return
            except _AttemptError as e:
                if e.retryable and attempt <= len(self._backoff):
                    log.debug("OTLP export attempt %d for span %s failed (%s) - retrying",
                              attempt, span.span_id_hex, e.detail)
                    await asyncio.sleep(self._backoff[attempt - 1])
                    continue
                raise ExportFailure(attempts=attempt, detail=e.detail) from None

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    def _client(self) -> aiohttp.ClientSession:
        # lazily bound to the running loop; the total timeout bounds one attempt
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=self.timeout_ms / 1000,
                connect=None if self.connect_timeout_ms is None else self.connect_timeout_ms / 1000)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _attempt(self, body: bytes) -> None:
        headers = {"content-type": CONTENT_TYPE, "accept": CONTENT_TYPE}
        # headers are resolved per export, never baked in: a credential published
        # after start-up takes effect without a restart
        for name, value in self._headers():
            headers[name] = value
        try:
            async with self._client().post(self.endpoint, data=body, headers=headers) as response:
                payload = await response.read()
                status = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            # the transport failed before an HTTP answer - retried like the Java
            # exporter retries every IOException
            raise _AttemptError(True, str(e) or e.__class__.__name__) from None
        _classify(status, payload)


def _classify(status: int, payload: bytes) -> None:
    if 200 <= status < 300:
        partial = otlp.partial_success(payload)
        if partial is not None:
            log.warning("OTLP backend accepted the request but rejected %d span(s) - %s",
                        partial.rejected_spans, partial.error_message)
        return
    text = payload.decode("utf-8", errors="replace")
    if status in RETRYABLE_STATUSES:
        raise _AttemptError(True, describe_http_failure(status, text))
    raise _AttemptError(False, describe_http_failure(status, text))


def describe_http_failure(status: int, body: str) -> str:
    """Render an HTTP rejection so it is actionable from one log line: the
    status, the backend's own explanation (bounded), and a hint for the usual
    causes."""
    text = f"HTTP {status}"
    collapsed = _collapse(body)
    if collapsed:
        text += " - " + collapsed
    hint = _HINTS.get(status)
    if hint:
        text += " | " + hint
    return text


_HINTS = {
    404: "check otel.exporter.otlp.endpoint includes the signal path (e.g. .../v1/traces), "
         "not just the vendor base URL",
    401: "the backend rejected the credential itself - check otel.exporter.otlp.headers (the "
         "header name and any auth scheme must match what the backend expects)",
    403: "the credential was accepted but lacks permission - grant the trace-ingest scope on "
         "the token (the response body above names it)",
    413: "the backend rejected the payload as too large",
    429: "the backend is rate-limiting; the exporter retries with backoff",
}


def _collapse(body: str) -> str:
    collapsed = " ".join(body.split())
    if len(collapsed) > MAX_BODY_CHARS:
        return collapsed[:MAX_BODY_CHARS] + "..."
    return collapsed
