"""
EventEnvelope and the standard wire format codec.

Implements the language-neutral standard format from the Mercury Composable
"Event Envelope Wire Format" reference: one MsgPack map with descriptive
string keys, no MsgPack extension types. Optional fields are omitted when
unset; absent and nil are equivalent; unknown keys are ignored; timestamps
travel as ISO-8601 UTC strings with millisecond precision.

The classic compact format (single-character map keys) is detected from the
first map key and rejected with :class:`CompactFormatError` — engines default
to the standard format for Event over HTTP.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, TypeAlias, cast

import msgpack

from .exceptions import CompactFormatError

# any MsgPack value - the payload universe of the standard wire format:
# a map, array, string, integer, float, boolean, binary or nil
Body: TypeAlias = "None | bool | int | float | str | bytes | list[Body] | dict[str, Body]"

# wire field names (standard format)
_ID = "id"
_TO = "to"
_FROM = "from"
_REPLY_TO = "reply_to"
_CID = "cid"
_TRACE_ID = "trace_id"
_TRACE_PATH = "trace_path"
_SPAN_ID = "span_id"
_STATUS = "status"
_HEADERS = "headers"
_BODY = "body"
_EXEC_TIME = "exec_time"
_ROUND_TRIP = "round_trip"
_TAGS = "tags"
_ANNOTATIONS = "annotations"
_STACK = "stack"
_OBJ_TYPE = "obj_type"
_EXCEPTION = "exception"


def iso_utc(dt: datetime | None = None) -> str:
    """ISO-8601 UTC with millisecond precision, e.g. 2026-07-21T12:00:00.000Z"""
    value = dt or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _pack_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return iso_utc(obj)
    raise TypeError(f"Cannot serialize type {type(obj).__name__} in event payload")


class EventEnvelope:
    """In-memory event with the same field vocabulary as the Java/Rust engines."""

    def __init__(self, to: str | None = None, body: Any = None,
                 headers: dict[str, str] | None = None):
        self.id: str = str(uuid.uuid4()).replace("-", "")
        self.to = to
        self.sender: str | None = None          # wire field "from"
        self.reply_to: str | None = None
        self.cid: str | None = None
        self.trace_id: str | None = None
        self.trace_path: str | None = None
        self.span_id: str | None = None
        self.status: int | None = None           # None encodes as absent (default 200)
        self.headers: dict[str, str] = dict(headers or {})
        self.body: Any = body
        self.exec_time: float | None = None
        self.round_trip: float | None = None
        self.tags: dict[str, str] = {}
        self.annotations: dict[str, Any] = {}
        self.stack: str | None = None
        self.obj_type: str | None = None
        self.exception: bytes | None = None       # language-native, opaque here

    # --- fluent helpers mirroring the engine API vocabulary ---

    def set_to(self, route: str) -> EventEnvelope:
        self.to = route
        return self

    def set_from(self, route: str) -> EventEnvelope:
        self.sender = route
        return self

    def set_header(self, key: str, value: Any) -> EventEnvelope:
        self.headers[str(key)] = str(value)
        return self

    def set_body(self, body: Any) -> EventEnvelope:
        self.body = body
        return self

    def set_status(self, status: int) -> EventEnvelope:
        self.status = int(status)
        return self

    def set_correlation_id(self, cid: str) -> EventEnvelope:
        self.cid = cid
        return self

    def set_trace(self, trace_id: str, trace_path: str) -> EventEnvelope:
        self.trace_id = trace_id
        self.trace_path = trace_path
        return self

    def set_span_id(self, span_id: str) -> EventEnvelope:
        self.span_id = span_id
        return self

    def set_reply_to(self, route: str | None) -> EventEnvelope:
        self.reply_to = route
        return self

    def get_status(self) -> int:
        return 200 if self.status is None else self.status

    def has_error(self) -> bool:
        return self.get_status() >= 400

    # --- wire codec (standard format) ---

    def to_map(self) -> dict[str, Any]:
        result: dict[str, Any] = {_ID: self.id, _HEADERS: dict(self.headers)}
        optional = [
            (_TO, self.to), (_FROM, self.sender), (_REPLY_TO, self.reply_to),
            (_CID, self.cid), (_TRACE_ID, self.trace_id), (_TRACE_PATH, self.trace_path),
            (_SPAN_ID, self.span_id), (_STATUS, self.status), (_BODY, self.body),
            (_EXEC_TIME, self.exec_time), (_ROUND_TRIP, self.round_trip),
            (_STACK, self.stack), (_OBJ_TYPE, self.obj_type), (_EXCEPTION, self.exception),
        ]
        for key, value in optional:
            if value is not None:
                result[key] = value
        if self.tags:
            result[_TAGS] = dict(self.tags)
        if self.annotations:
            result[_ANNOTATIONS] = dict(self.annotations)
        return result

    def to_bytes(self) -> bytes:
        # packb returns None only in the legacy stream mode - never here
        return cast(bytes, msgpack.packb(self.to_map(), use_bin_type=True, default=_pack_default))

    @classmethod
    def from_map(cls, data: dict[str, Any]) -> EventEnvelope:
        event = cls()
        raw_id: Any = data.get(_ID)
        if raw_id is not None:
            event.id = str(raw_id)
        event.to = data.get(_TO)
        event.sender = data.get(_FROM)
        event.reply_to = data.get(_REPLY_TO)
        event.cid = data.get(_CID)
        event.trace_id = data.get(_TRACE_ID)
        event.trace_path = data.get(_TRACE_PATH)
        event.span_id = data.get(_SPAN_ID)
        raw_status: Any = data.get(_STATUS)
        if raw_status is not None:
            event.status = int(raw_status)
        raw_headers = data.get(_HEADERS)
        if isinstance(raw_headers, dict):
            event.headers = {str(k): str(v) for k, v in raw_headers.items()}
        event.body = data.get(_BODY)
        raw_exec_time: Any = data.get(_EXEC_TIME)
        if raw_exec_time is not None:
            event.exec_time = float(raw_exec_time)
        raw_round_trip: Any = data.get(_ROUND_TRIP)
        if raw_round_trip is not None:
            event.round_trip = float(raw_round_trip)
        raw_tags = data.get(_TAGS)
        if isinstance(raw_tags, dict):
            event.tags = {str(k): str(v) for k, v in raw_tags.items()}
        raw_annotations = data.get(_ANNOTATIONS)
        if isinstance(raw_annotations, dict):
            event.annotations = dict(raw_annotations)
        event.stack = data.get(_STACK)
        event.obj_type = data.get(_OBJ_TYPE)
        raw_exception = data.get(_EXCEPTION)
        if isinstance(raw_exception, (bytes, bytearray)):
            event.exception = bytes(raw_exception)
        return event

    @classmethod
    def from_bytes(cls, data: bytes) -> EventEnvelope:
        try:
            decoded = msgpack.unpackb(data, raw=False)
        except Exception as e:
            raise ValueError(f"Unable to decode event envelope - {e}") from e
        if not isinstance(decoded, dict) or not decoded:
            raise ValueError("Unable to decode event envelope - not a MsgPack map")
        first_key = next(iter(decoded))
        if isinstance(first_key, str) and len(first_key) == 1:
            raise CompactFormatError(
                "Compact event envelope format is not supported - "
                "use the standard format (event.over.http.format=standard)")
        return cls.from_map(decoded)

    def __repr__(self) -> str:
        return (f"EventEnvelope(id='{self.id}', to='{self.to or ''}', "
                f"status={self.get_status()}, headers={self.headers})")
