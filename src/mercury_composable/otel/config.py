"""
The forwarder's configuration: the engines' ``otel.*`` keys, read the engines'
way (the Rust port's ``config.rs`` and ``headers.rs``).

======================================= ================================================ =========================
Key                                     Meaning                                          Default
======================================= ================================================ =========================
``otel.forwarding``                     the master switch                                ``false``
``otel.exporter.otlp.endpoint``         the OTLP/HTTP traces URL incl. the signal path   ``http://localhost:4318/v1/traces``
``otel.exporter.otlp.timeout``          per-export timeout, milliseconds                 ``10000``
``otel.exporter.otlp.connect.timeout``  connect timeout, milliseconds (honoured here)    the client default
``otel.exporter.otlp.headers``          request headers: ``k=v`` or ``k: v``, comma list (none)
``otel.exporter.otlp.compression``      only ``none`` is honoured (a warning otherwise)  ``none``
``otel.service.name``                   the ``service.name`` resource attribute          ``application.name``
======================================= ================================================ =========================
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..log import get_logger
from ..version import __version__

log = get_logger("mercury.otel")

FORWARDING_SWITCH = "otel.forwarding"
ENDPOINT = "otel.exporter.otlp.endpoint"
TIMEOUT = "otel.exporter.otlp.timeout"
CONNECT_TIMEOUT = "otel.exporter.otlp.connect.timeout"
COMPRESSION = "otel.exporter.otlp.compression"
HEADERS = "otel.exporter.otlp.headers"
SERVICE_NAME = "otel.service.name"
APP_NAME = "application.name"
APP_VERSION = "info.app.version"

DEFAULT_ENDPOINT = "http://localhost:4318/v1/traces"
DEFAULT_TIMEOUT_MS = 10_000
DEFAULT_COMPRESSION = "none"
DEFAULT_SERVICE = "mercury"

# the request headers, resolved per export
HeaderSupplier = Callable[[], list[tuple[str, str]]]


@dataclass
class ForwarderSettings:
    endpoint: str
    service_name: str
    scope_version: str
    timeout_ms: int
    connect_timeout_ms: int | None
    compression: str
    headers: HeaderSupplier

    @classmethod
    def from_config(cls, config: Any) -> ForwarderSettings:
        """Read the ``otel.*`` keys. Infallible: an unparseable number falls back
        to its default with a warning; the endpoint URL is validated when the
        exporter is built."""
        service_name = config.get_property(SERVICE_NAME) or config.get_property(APP_NAME) \
            or DEFAULT_SERVICE
        endpoint = config.get_property(ENDPOINT) or DEFAULT_ENDPOINT
        timeout_ms = _millis(config.get_property(TIMEOUT), TIMEOUT, DEFAULT_TIMEOUT_MS)
        connect_timeout_ms = None
        if config.exists(CONNECT_TIMEOUT):
            connect_timeout_ms = _millis(config.get_property(CONNECT_TIMEOUT), CONNECT_TIMEOUT,
                                         DEFAULT_TIMEOUT_MS)
        compression = (config.get_property(COMPRESSION) or "").strip() or DEFAULT_COMPRESSION
        if compression.lower() != DEFAULT_COMPRESSION:
            log.warning("%s=%s is not supported by this host - exporting uncompressed (the payload "
                        "is one span per request); set none to silence this", COMPRESSION,
                        compression)
        scope_version = config.get_property(APP_VERSION) or __version__

        # read through a supplier so a credential published AFTER this start-up
        # read (a runtime override, the engines' -D / config.set analog a
        # credential bootstrap uses) is picked up rather than frozen out
        def raw_headers() -> str | None:
            return config.get_property(HEADERS)

        return cls(endpoint=endpoint, service_name=service_name, scope_version=scope_version,
                   timeout_ms=timeout_ms, connect_timeout_ms=connect_timeout_ms,
                   compression=compression, headers=reloading_headers(raw_headers))

    @classmethod
    def fixed(cls, endpoint: str, timeout_ms: int = DEFAULT_TIMEOUT_MS,
              headers: list[tuple[str, str]] | None = None,
              service_name: str = DEFAULT_SERVICE) -> ForwarderSettings:
        """Settings with a FIXED header list - for tests and callers whose
        credentials are known up front."""
        fixed_headers = list(headers or [])
        return cls(endpoint=endpoint, service_name=service_name, scope_version=__version__,
                   timeout_ms=timeout_ms, connect_timeout_ms=None,
                   compression=DEFAULT_COMPRESSION, headers=lambda: list(fixed_headers))


def reloading_headers(raw: Callable[[], str | None]) -> HeaderSupplier:
    """A supplier that re-parses the raw header setting on every call and
    announces the header NAMES once when they first resolve (never values)."""
    announced = {"done": False}

    def supply() -> list[tuple[str, str]]:
        headers = parse_headers(raw())
        if headers and not announced["done"]:
            announced["done"] = True
            log.info("OTLP credential header resolved - %s", [k for k, _ in headers])
        return headers

    return supply


def parse_headers(raw: str | None) -> list[tuple[str, str]]:
    """The OpenTelemetry ``key=value,key2=value2`` list, also accepting the
    ``key: value`` form; the FIRST separator splits, so a token containing
    ``=`` or ``:`` survives; a repeated name keeps its last value."""
    out: list[tuple[str, str]] = []
    if raw is None or not raw.strip() or raw.strip() == "null":
        return out
    for pair in raw.split(","):
        sep = _first_separator(pair)
        if sep is None:
            continue
        key = pair[:sep].strip()
        value = pair[sep + 1:].strip()
        if not key:
            continue
        for i, (k, _) in enumerate(out):
            if k == key:
                out[i] = (key, value)
                break
        else:
            out.append((key, value))
    return out


def _first_separator(pair: str) -> int | None:
    eq = pair.find("=")
    colon = pair.find(":")
    if eq < 0 and colon < 0:
        return None
    if eq < 0:
        sep = colon
    elif colon < 0:
        sep = eq
    else:
        sep = min(eq, colon)
    return sep if sep > 0 else None


def _millis(value: str | None, key: str, default: int) -> int:
    if value is None:
        return default
    try:
        ms = int(value.strip())
    except ValueError:
        ms = 0
    if ms > 0:
        return ms
    log.warning("%s=%s is not a positive number of milliseconds - using %d", key, value, default)
    return default
