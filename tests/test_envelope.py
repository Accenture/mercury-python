"""Codec unit tests: round-trip, omission rules, format detection, timestamps."""

import base64
import json
import os
from datetime import datetime, timezone

import msgpack
import pytest

from mercury_composable import CompactFormatError, EventEnvelope, iso_utc

VECTORS = os.path.join(os.path.dirname(__file__), "vectors", "vectors.json")


def test_round_trip_all_fields():
    event = (EventEnvelope(to="target.route", body={"a": 1, "b": [1, 2, 3]})
             .set_from("source.route").set_reply_to("reply.route")
             .set_correlation_id("cid-1").set_trace("trace-1", "GET /api/x")
             .set_status(200).set_header("k1", "v1"))
    event.span_id = "span-1"
    event.exec_time = 1.5
    event.round_trip = 2.5
    event.tags = {"rpc": "30000"}
    event.annotations = {"note": "n1"}
    event.stack = "trace text"
    event.obj_type = "com.example.Demo"
    decoded = EventEnvelope.from_bytes(event.to_bytes())
    assert decoded.to == "target.route"
    assert decoded.sender == "source.route"
    assert decoded.reply_to == "reply.route"
    assert decoded.cid == "cid-1"
    assert decoded.trace_id == "trace-1"
    assert decoded.trace_path == "GET /api/x"
    assert decoded.span_id == "span-1"
    assert decoded.get_status() == 200
    assert decoded.headers == {"k1": "v1"}
    assert decoded.body == {"a": 1, "b": [1, 2, 3]}
    assert decoded.exec_time == 1.5
    assert decoded.round_trip == 2.5
    assert decoded.tags == {"rpc": "30000"}
    assert decoded.annotations == {"note": "n1"}
    assert decoded.stack == "trace text"
    assert decoded.obj_type == "com.example.Demo"


def test_unset_fields_are_omitted_and_headers_id_always_present():
    wire = msgpack.unpackb(EventEnvelope().to_bytes(), raw=False)
    assert set(wire.keys()) == {"id", "headers"}
    assert wire["headers"] == {}


def test_absent_and_nil_are_equivalent():
    explicit_nil = msgpack.packb({"id": "x1", "headers": {}, "body": None}, use_bin_type=True)
    decoded = EventEnvelope.from_bytes(explicit_nil)
    assert decoded.body is None
    assert decoded.get_status() == 200  # default when unset


def test_unknown_keys_are_ignored():
    payload = msgpack.packb({"id": "x2", "headers": {}, "future_field": 42}, use_bin_type=True)
    decoded = EventEnvelope.from_bytes(payload)
    assert decoded.id == "x2"


def test_compact_format_detected_and_rejected():
    compact = msgpack.packb({"0": "e1", "T": "hello.world"}, use_bin_type=True)
    with pytest.raises(CompactFormatError):
        EventEnvelope.from_bytes(compact)


def test_binary_body():
    event = EventEnvelope(to="bin.route", body=b"\x00\x01\x02")
    decoded = EventEnvelope.from_bytes(event.to_bytes())
    assert decoded.body == b"\x00\x01\x02"


def test_datetime_encodes_as_iso_utc_string():
    moment = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
    assert iso_utc(moment) == "2026-07-21T12:00:00.000Z"
    event = EventEnvelope(to="time.route", body={"when": moment})
    decoded = EventEnvelope.from_bytes(event.to_bytes())
    assert decoded.body["when"] == "2026-07-21T12:00:00.000Z"


def _decoded_wire_fields(envelope: EventEnvelope) -> dict:
    """Envelope -> the wire map for semantic comparison with a vector's expect."""
    return envelope.to_map()


def test_golden_vectors_conformance():
    with open(VECTORS, "r", encoding="utf-8") as f:
        catalog = json.load(f)
    standard = [v for v in catalog["vectors"] if v["format"] == "standard"]
    compact = [v for v in catalog["vectors"] if v["format"] == "compact"]
    assert standard and compact
    for vector in standard:
        raw = base64.b64decode(vector["base64"])
        decoded = EventEnvelope.from_bytes(raw)
        wire = _decoded_wire_fields(decoded)
        for key, expected in vector["expect"].items():
            assert wire.get(key) == expected, f"{vector['name']}: field '{key}'"
        # re-encode and decode again: semantic equality (byte identity not required)
        again = _decoded_wire_fields(EventEnvelope.from_bytes(decoded.to_bytes()))
        for key, expected in vector["expect"].items():
            assert again.get(key) == expected, f"{vector['name']} re-encoded: field '{key}'"
    for vector in compact:
        with pytest.raises(CompactFormatError):
            EventEnvelope.from_bytes(base64.b64decode(vector["base64"]))
