"""
The OTLP wire format - a hand-written protobuf encoder for the one message the
forwarder sends, ``ExportTraceServiceRequest``, and a reader for the one it
receives, ``ExportTraceServiceResponse`` (the Rust port's ``otlp.rs``, line for
line).

No protobuf library, no generated code, no OpenTelemetry SDK: the OTLP v1 trace
schema is frozen and the forwarder needs eight message types with scalar fields.
Field numbers are the OTLP ``trace.proto`` / ``common.proto`` / ``resource.proto``
/ ``trace_service.proto`` definitions; the encoding rules are
https://protobuf.dev/programming-guides/encoding/ ::

    ExportTraceServiceRequest { repeated ResourceSpans resource_spans = 1; }
    ResourceSpans   { Resource resource = 1; repeated ScopeSpans scope_spans = 2; }
    Resource        { repeated KeyValue attributes = 1; }
    ScopeSpans      { InstrumentationScope scope = 1; repeated Span spans = 2; }
    InstrumentationScope { string name = 1; string version = 2; }
    KeyValue        { string key = 1; AnyValue value = 2; }
    AnyValue        { oneof value { string string_value = 1; bool bool_value = 2;
                                    int64 int_value = 3; double double_value = 4; } }
    Span            { bytes trace_id = 1; bytes span_id = 2; bytes parent_span_id = 4;
                      string name = 5; SpanKind kind = 6; fixed64 start_time_unix_nano = 7;
                      fixed64 end_time_unix_nano = 8; repeated KeyValue attributes = 9;
                      Status status = 15; fixed32 flags = 16; }
    Status          { string message = 2; StatusCode code = 3; }
    ExportTraceServiceResponse { ExportTracePartialSuccess partial_success = 1; }
    ExportTracePartialSuccess  { int64 rejected_spans = 1; string error_message = 2; }
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .span import STATUS_UNSET, AttributeValue, Span

# protobuf wire types (the low 3 bits of a field tag)
WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5

# OTLP span flags: bits 0-7 are the W3C trace flags (0x01 = sampled), bit 8 says
# the is-remote bit is known, bit 9 is the is-remote bit itself. A span this host
# produced is sampled and local - the value the Java SDK writes.
SPAN_FLAGS_SAMPLED_LOCAL = 0x0101


class ProtoWriter:
    """A minimal protobuf writer: field tags, varints, fixed-width scalars and
    length-delimited payloads. Proto3 default values are omitted, except for
    ``oneof`` members (which have explicit presence) - the ``*_always`` methods."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def into_bytes(self) -> bytes:
        return bytes(self._buf)

    def varint(self, value: int) -> None:
        """Base-128 varint: little-endian groups of 7 bits, high bit = more follows."""
        if value < 0:
            value &= (1 << 64) - 1  # two's complement, 64-bit
        while True:
            byte = value & 0x7F
            value >>= 7
            if value == 0:
                self._buf.append(byte)
                return
            self._buf.append(byte | 0x80)

    def _tag(self, field: int, wire: int) -> None:
        self.varint((field << 3) | wire)

    def string(self, field: int, value: str) -> None:
        """``string`` - omitted when empty (proto3 default)."""
        if value:
            self.bytes(field, value.encode("utf-8"))

    def string_always(self, field: int, value: str) -> None:
        """A ``oneof`` string member - always written, even when empty."""
        data = value.encode("utf-8")
        self._tag(field, WIRE_LEN)
        self.varint(len(data))
        self._buf.extend(data)

    def bytes(self, field: int, value: bytes) -> None:
        """``bytes`` - omitted when empty."""
        if value:
            self._tag(field, WIRE_LEN)
            self.varint(len(value))
            self._buf.extend(value)

    def message(self, field: int, body: bytes) -> None:
        """An embedded message - always written (an empty message is meaningful)."""
        self._tag(field, WIRE_LEN)
        self.varint(len(body))
        self._buf.extend(body)

    def uint(self, field: int, value: int) -> None:
        """``uint32`` / ``uint64`` / enum - omitted when zero."""
        if value != 0:
            self._tag(field, WIRE_VARINT)
            self.varint(value)

    def int64_always(self, field: int, value: int) -> None:
        """A ``oneof`` ``int64`` member - always written (two's complement varint)."""
        self._tag(field, WIRE_VARINT)
        self.varint(value)

    def fixed64(self, field: int, value: int) -> None:
        """``fixed64`` - omitted when zero."""
        if value != 0:
            self._tag(field, WIRE_FIXED64)
            self._buf.extend(struct.pack("<Q", value))

    def double_always(self, field: int, value: float) -> None:
        """A ``oneof`` ``double`` member - always written (IEEE-754, little-endian)."""
        self._tag(field, WIRE_FIXED64)
        self._buf.extend(struct.pack("<d", value))

    def fixed32(self, field: int, value: int) -> None:
        """``fixed32`` - omitted when zero."""
        if value != 0:
            self._tag(field, WIRE_FIXED32)
            self._buf.extend(struct.pack("<I", value))


def _any_value(value: AttributeValue) -> bytes:
    w = ProtoWriter()
    if isinstance(value, bool):
        # bool_value = 2 (a oneof member: written even when false); the mapping
        # emits no booleans today, the branch keeps the oneof complete
        w.int64_always(2, 1 if value else 0)
    elif isinstance(value, int):
        w.int64_always(3, value)
    elif isinstance(value, float):
        w.double_always(4, value)
    else:
        w.string_always(1, str(value))
    return w.into_bytes()


def _key_value(key: str, value: AttributeValue) -> bytes:
    w = ProtoWriter()
    w.string(1, key)
    w.message(2, _any_value(value))
    return w.into_bytes()


def _resource(service_name: str) -> bytes:
    w = ProtoWriter()
    w.message(1, _key_value("service.name", service_name))
    return w.into_bytes()


def _instrumentation_scope(name: str, version: str) -> bytes:
    w = ProtoWriter()
    w.string(1, name)
    w.string(2, version)
    return w.into_bytes()


def _status(span: Span) -> bytes | None:
    if span.status_code == STATUS_UNSET and not span.status_message:
        return None
    w = ProtoWriter()
    w.string(2, span.status_message)
    w.uint(3, span.status_code)
    return w.into_bytes()


def span_message(span: Span) -> bytes:
    """The OTLP ``Span`` message for one mapped span."""
    w = ProtoWriter()
    w.bytes(1, span.trace_id)
    w.bytes(2, span.span_id)
    if span.parent_span_id is not None:
        w.bytes(4, span.parent_span_id)
    w.string(5, span.name)
    w.uint(6, span.kind)
    w.fixed64(7, span.start_unix_nano)
    w.fixed64(8, span.end_unix_nano)
    for key, value in span.attributes:
        w.message(9, _key_value(key, value))
    status = _status(span)
    if status is not None:
        w.message(15, status)
    w.fixed32(16, SPAN_FLAGS_SAMPLED_LOCAL)
    return w.into_bytes()


def encode_export_request(service_name: str, scope_name: str, scope_version: str,
                          span: Span) -> bytes:
    """One ``ExportTraceServiceRequest`` carrying one span under one resource and
    one instrumentation scope - the request body of an OTLP/HTTP export."""
    scope_spans = ProtoWriter()
    scope_spans.message(1, _instrumentation_scope(scope_name, scope_version))
    scope_spans.message(2, span_message(span))
    resource_spans = ProtoWriter()
    resource_spans.message(1, _resource(service_name))
    resource_spans.message(2, scope_spans.into_bytes())
    request = ProtoWriter()
    request.message(1, resource_spans.into_bytes())
    return request.into_bytes()


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


class ProtoReader:
    """A minimal, bounds-checked protobuf reader - enough to walk the OTLP
    message tree (the response's ``partial_success``, and the test collector's
    decoding of what this host sent). Every read returns ``None`` past the end
    instead of raising, so a malformed body is reported, never fatal."""

    def __init__(self, buf: bytes) -> None:
        self._buf = buf
        self._pos = 0

    def has_more(self) -> bool:
        return self._pos < len(self._buf)

    def read_tag(self) -> tuple[int, int] | None:
        """The next field tag as ``(field_number, wire_type)``."""
        tag = self.read_varint()
        if tag is None:
            return None
        return tag >> 3, tag & 0x7

    def read_varint(self) -> int | None:
        result = 0
        shift = 0
        while True:
            if self._pos >= len(self._buf):
                return None
            byte = self._buf[self._pos]
            self._pos += 1
            if shift > 63:
                return None
            result |= (byte & 0x7F) << shift
            if byte & 0x80 == 0:
                return result
            shift += 7

    def read_fixed64(self) -> int | None:
        if self._pos + 8 > len(self._buf):
            return None
        (value,) = struct.unpack_from("<Q", self._buf, self._pos)
        self._pos += 8
        return int(value)

    def read_double(self) -> float | None:
        if self._pos + 8 > len(self._buf):
            return None
        (value,) = struct.unpack_from("<d", self._buf, self._pos)
        self._pos += 8
        return float(value)

    def read_fixed32(self) -> int | None:
        if self._pos + 4 > len(self._buf):
            return None
        (value,) = struct.unpack_from("<I", self._buf, self._pos)
        self._pos += 4
        return int(value)

    def read_bytes(self) -> bytes | None:
        """A length-delimited chunk: bytes, a string, or an embedded message."""
        length = self.read_varint()
        if length is None or self._pos + length > len(self._buf):
            return None
        out = self._buf[self._pos:self._pos + length]
        self._pos += length
        return out

    def read_string(self) -> str | None:
        data = self.read_bytes()
        return None if data is None else data.decode("utf-8", errors="replace")

    def skip(self, wire: int) -> bool:
        """Advance past a field whose value is not needed, honouring its wire type."""
        if wire == WIRE_VARINT:
            return self.read_varint() is not None
        if wire == WIRE_FIXED64:
            return self.read_fixed64() is not None
        if wire == WIRE_LEN:
            return self.read_bytes() is not None
        if wire == WIRE_FIXED32:
            return self.read_fixed32() is not None
        return False


@dataclass
class PartialSuccess:
    """The ``partial_success`` block of an ``ExportTraceServiceResponse``: a
    backend that accepted the request but rejected spans says so here."""
    rejected_spans: int = 0
    error_message: str = ""


def partial_success(body: bytes) -> PartialSuccess | None:
    """The response's partial-success block when it carries content, else ``None``."""
    reader = ProtoReader(body)
    result: PartialSuccess | None = None
    while reader.has_more():
        tag = reader.read_tag()
        if tag is None:
            return None
        field, wire = tag
        if field == 1 and wire == WIRE_LEN:
            block = reader.read_bytes()
            if block is None:
                return None
            inner = ProtoReader(block)
            partial = PartialSuccess()
            while inner.has_more():
                inner_tag = inner.read_tag()
                if inner_tag is None:
                    return None
                f, w = inner_tag
                if f == 1 and w == WIRE_VARINT:
                    value = inner.read_varint()
                    if value is None:
                        return None
                    partial.rejected_spans = value if value < (1 << 63) else value - (1 << 64)
                elif f == 2 and w == WIRE_LEN:
                    text = inner.read_string()
                    if text is None:
                        return None
                    partial.error_message = text
                elif not inner.skip(w):
                    return None
            result = partial
        elif not reader.skip(wire):
            return None
    if result is not None and (result.rejected_spans != 0 or result.error_message):
        return result
    return None
