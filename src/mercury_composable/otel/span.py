"""
The telemetry dataset -> OpenTelemetry span mapping (the engines'
``TraceMetricsSpanData`` / the Rust port's ``span.rs``).

The host has already produced the W3C-compatible trace id, span id and parent
span id during execution, so the span built here carries those **exact** ids -
an OpenTelemetry tracer would mint new ones and break the lineage. A dataset
whose ids are not W3C-valid (32 / 16 lowercase hex, not all zeros) is skipped
rather than exported with forged ids.

======================== ==============================================================
Dataset metric           Span
======================== ==============================================================
``id``                   trace id
``span_id``              span id
``parent_span_id``       parent span id (a root span when absent)
``service`` (route)      span name (``path``, then ``task``, when absent)
``start`` + ``exec_time`` start / end timestamps
``success`` / ``status`` / ``exception``  status OK, or ERROR with a description
``service`` = http.request  kind SERVER - the edge's round-trip record; a function
                         execution is INTERNAL (its ``from`` does not decide the kind)
``path``, ``from``, ``origin``, ``status``, ``exec_time_ms``, ``round_trip_ms``,
``exception``            attributes (same names); ``service`` is the ``route`` attribute
``annotations`` entries  ``annotation.<key>`` attributes
======================== ==============================================================
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

NANOS_PER_MILLI = 1_000_000.0
HTTP_REQUEST = "http.request"

# OTLP SpanKind - only the two values the datasets produce
KIND_INTERNAL = 1
KIND_SERVER = 2

# OTLP StatusCode
STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2

# an OTLP AnyValue - the scalar shapes the mapping emits
AttributeValue = str | int | float


@dataclass
class Span:
    """One completed span, ready for the OTLP encoder."""
    trace_id: bytes
    span_id: bytes
    parent_span_id: bytes | None
    name: str
    kind: int
    start_unix_nano: int
    end_unix_nano: int
    attributes: list[tuple[str, AttributeValue]] = field(default_factory=list)
    status_code: int = STATUS_UNSET
    status_message: str = ""

    @property
    def trace_id_hex(self) -> str:
        return self.trace_id.hex()

    @property
    def span_id_hex(self) -> str:
        return self.span_id.hex()

    @property
    def parent_span_id_hex(self) -> str | None:
        return None if self.parent_span_id is None else self.parent_span_id.hex()

    def attribute(self, key: str) -> AttributeValue | None:
        for k, v in self.attributes:
            if k == key:
                return v
        return None


def span_from_dataset(dataset: Any) -> Span | None:
    """Map one telemetry dataset (``{"trace": {...}, "annotations": {...}}``)
    to a span - ``None`` when the dataset has no ``trace`` block or its trace
    / span id is not W3C-valid."""
    if not isinstance(dataset, dict):
        return None
    trace = dataset.get("trace")
    if not isinstance(trace, dict):
        return None
    trace_id = _hex_id(trace.get("id"), 16)
    span_id = _hex_id(trace.get("span_id"), 8)
    if trace_id is None or span_id is None:
        return None
    notes = dataset.get("annotations")
    start_unix_nano = _start_unix_nano(trace.get("start"))
    exec_ms = _to_float(trace.get("exec_time"))
    status_code, status_message = _span_status(trace)
    service = _display(trace.get("service"))
    path = _display(trace.get("path"))
    # the edge's round-trip record (service "http.request", emitted by an engine's REST
    # automation when the response completes) is the SERVER span; every function execution
    # - including the first one, whose "from" is http.request - is an INTERNAL hop under it
    kind = KIND_SERVER if service == HTTP_REQUEST else KIND_INTERNAL
    return Span(trace_id=trace_id, span_id=span_id,
                parent_span_id=_hex_id(trace.get("parent_span_id"), 8),
                name=service or path or "task", kind=kind, start_unix_nano=start_unix_nano,
                end_unix_nano=start_unix_nano + int(exec_ms * NANOS_PER_MILLI),
                attributes=_span_attributes(trace, notes if isinstance(notes, dict) else {},
                                            service, path, exec_ms),
                status_code=status_code, status_message=status_message)


def _start_unix_nano(value: Any) -> int:
    """The dataset's ``start`` as nanoseconds since the epoch; now when it is missing or
    does not parse."""
    text = _display(value)
    nanos = parse_iso8601_nanos(text) if text is not None else None
    return time.time_ns() if nanos is None else nanos


def _span_status(trace: dict[str, Any]) -> tuple[int, str]:
    """The span status: OK for a success, else ERROR described by the exception, or by the
    status."""
    if _to_bool(trace.get("success")):
        return STATUS_OK, ""
    exception = _display(trace.get("exception"))
    if exception is not None:
        return STATUS_ERROR, exception
    return STATUS_ERROR, f"status={_display(trace.get('status')) or 'null'}"


def _span_attributes(trace: dict[str, Any], notes: dict[str, Any], service: str | None,
                     path: str | None, exec_ms: float) -> list[tuple[str, AttributeValue]]:
    """The span attributes: the trace metrics under the same names (``service`` as
    ``route``) and each annotation as ``annotation.<key>``."""
    attributes: list[tuple[str, AttributeValue]] = []

    def put_str(key: str, content: str | None) -> None:
        if content is not None:
            attributes.append((key, content))

    put_str("route", service)
    put_str("from", _display(trace.get("from")))
    put_str("origin", _display(trace.get("origin")))
    put_str("path", path)
    if trace.get("status") is not None:
        attributes.append(("status", _to_int(trace.get("status"))))
    attributes.append(("exec_time_ms", exec_ms))
    if trace.get("round_trip") is not None:
        attributes.append(("round_trip_ms", _to_float(trace.get("round_trip"))))
    put_str("exception", _display(trace.get("exception")))
    for key, value in notes.items():
        put_str(f"annotation.{key}", _display(value))
    return attributes


def _display(value: Any) -> str | None:
    """Java ``String.valueOf(value)``: text for scalars, JSON for structures,
    ``None`` for a missing value."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, dict | list):
        try:
            return json.dumps(value, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _to_float(value: Any) -> float:
    """Java ``toDouble``: a number, a parseable string, else 0."""
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return 0.0
    return 0.0


def _to_int(value: Any) -> int:
    """Java ``toLong``: a number's integer value, a parseable string, else 0."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return 0
    return 0


def _to_bool(value: Any) -> bool:
    """Java ``toBool``: a boolean, or the text ``true`` (case-insensitive); a
    missing value means success."""
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _hex_id(value: Any, size: int) -> bytes | None:
    """A W3C id: exactly ``2 * size`` lowercase hex digits, not all zeros."""
    text = _display(value)
    if text is None or len(text) != 2 * size:
        return None
    if any(c not in "0123456789abcdef" for c in text):
        return None
    raw = bytes.fromhex(text)
    return None if not any(raw) else raw


def parse_iso8601_nanos(text: str) -> int | None:
    """Parse an ISO-8601 UTC instant (``YYYY-MM-DDTHH:MM:SS[.fraction]Z``, the
    shape the engines and this host write) to nanoseconds since the Unix epoch
    - the inverse of the envelope formatter, with no date library (days from
    civil per Howard Hinnant)."""
    b = text.strip()
    fields = _date_time_fields(b)
    if fields is None:
        return None
    year, month, day, hour, minute, second = fields
    pos, nanos = 19, 0
    if b[pos] == ".":
        fraction = _fraction_nanos(b, pos + 1)
        if fraction is None:
            return None
        nanos, pos = fraction
    if pos + 1 != len(b) or b[pos] not in "Zz":
        return None
    days = _days_from_civil(year, month, day)
    secs = days * 86_400 + hour * 3600 + minute * 60 + second
    if secs < 0:
        return None
    return secs * 1_000_000_000 + nanos


def _date_time_fields(b: str) -> tuple[int, int, int, int, int, int] | None:
    """The six fields of the ``YYYY-MM-DDTHH:MM:SS`` head of ``b``, range-checked; ``None``
    when the text is too short or not in that shape."""
    if len(b) < 20:
        return None
    if b[4] != "-" or b[7] != "-" or b[10] not in "Tt" or b[13] != ":" or b[16] != ":":
        return None
    parts = (b[0:4], b[5:7], b[8:10], b[11:13], b[14:16], b[17:19])
    if not all(p.isdigit() for p in parts):
        return None
    year, month, day, hour, minute, second = (int(p) for p in parts)
    if not 1 <= month <= 12 or not 1 <= day <= 31 or hour > 23 or minute > 59 or second > 60:
        return None
    return year, month, day, hour, minute, second


def _fraction_nanos(b: str, start: int) -> tuple[int, int] | None:
    """The fraction digits from ``start`` as nanoseconds, with the position after them;
    ``None`` when there is no digit or there are more than nine."""
    pos = start
    while pos < len(b) and b[pos].isdigit():
        pos += 1
    digits = b[start:pos]
    if not digits or len(digits) > 9:
        return None
    return int(digits.ljust(9, "0")), pos


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 for a proleptic Gregorian date."""
    y = year - 1 if month <= 2 else year
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    mp = month - 3 if month > 2 else month + 9
    doy = (153 * mp + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146_097 + doe - 719_468
