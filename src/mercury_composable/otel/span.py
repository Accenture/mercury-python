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
``from`` = http.request  kind SERVER (else INTERNAL)
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
    parent_span_id = _hex_id(trace.get("parent_span_id"), 8)
    annotations = dataset.get("annotations")
    if not isinstance(annotations, dict):
        annotations = {}
    start_text = _display(trace.get("start"))
    start_unix_nano = parse_iso8601_nanos(start_text) if start_text is not None else None
    if start_unix_nano is None:
        start_unix_nano = time.time_ns()
    exec_ms = _to_float(trace.get("exec_time"))
    end_unix_nano = start_unix_nano + int(exec_ms * NANOS_PER_MILLI)
    if _to_bool(trace.get("success")):
        status_code, status_message = STATUS_OK, ""
    else:
        status_code = STATUS_ERROR
        exception = _display(trace.get("exception"))
        if exception is not None:
            status_message = exception
        else:
            status_message = f"status={_display(trace.get('status')) or 'null'}"
    service = _display(trace.get("service"))
    path = _display(trace.get("path"))
    name = service or path or "task"
    kind = KIND_SERVER if _display(trace.get("from")) == HTTP_REQUEST else KIND_INTERNAL
    attributes: list[tuple[str, AttributeValue]] = []

    def put_str(key: str, value: str | None) -> None:
        if value is not None:
            attributes.append((key, value))

    put_str("route", service)
    put_str("from", _display(trace.get("from")))
    put_str("origin", _display(trace.get("origin")))
    put_str("path", path)
    if trace.get("status") is not None:
        attributes.append(("status", _to_int(trace.get("status"))))
    attributes.append(("exec_time_ms", exec_ms))
    if trace.get("round_trip") is not None:
        attributes.append(("round_trip_ms", _to_float(trace.get("round_trip"))))
    exception_text = _display(trace.get("exception"))
    if exception_text is not None:
        attributes.append(("exception", exception_text))
    for key, value in annotations.items():
        text = _display(value)
        if text is not None:
            attributes.append((f"annotation.{key}", text))
    return Span(trace_id=trace_id, span_id=span_id, parent_span_id=parent_span_id,
                name=name, kind=kind, start_unix_nano=start_unix_nano,
                end_unix_nano=end_unix_nano, attributes=attributes,
                status_code=status_code, status_message=status_message)


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
    pos = 19
    nanos = 0
    if b[pos] == ".":
        pos += 1
        start = pos
        while pos < len(b) and b[pos].isdigit():
            pos += 1
        digits = b[start:pos]
        if not digits or len(digits) > 9:
            return None
        nanos = int(digits.ljust(9, "0"))
    if pos + 1 != len(b) or b[pos] not in "Zz":
        return None
    days = _days_from_civil(year, month, day)
    secs = days * 86_400 + hour * 3600 + minute * 60 + second
    if secs < 0:
        return None
    return secs * 1_000_000_000 + nanos


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 for a proleptic Gregorian date."""
    y = year - 1 if month <= 2 else year
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    mp = month - 3 if month > 2 else month + 9
    doy = (153 * mp + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146_097 + doe - 719_468
