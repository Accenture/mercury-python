"""
The OpenTelemetry trace forwarder - the engines' ``opentelemetry-forwarder``
extension for this host, opt-in and dependency-free.

Every traced, non-RPC execution already emits the engines' distributed-trace
dataset on the ``distributed.tracing`` log stream. With ``otel.forwarding=true``
the host ALSO hands each dataset to a function on the engines' extension route
``distributed.trace.forwarder``; the built-in forwarder registered there maps
the dataset to one OpenTelemetry span carrying the host's exact W3C ids and
exports it over OTLP/HTTP (protobuf) to the configured endpoint - Dynatrace,
Splunk, an OpenTelemetry Collector - so one trace spans the engines and the
polyglot functions they call. Off by default: the switch is the only thing
that turns it on.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..log import get_logger
from ..trace import DISTRIBUTED_TRACE_FORWARDER
from .config import FORWARDING_SWITCH, HEADERS, ForwarderSettings, parse_headers
from .export import INSTRUMENTATION_SCOPE, Exporter, ExportFailure, describe_http_failure
from .span import Span, span_from_dataset

__all__ = [
    "DISTRIBUTED_TRACE_FORWARDER",
    "FORWARDING_SWITCH",
    "INSTRUMENTATION_SCOPE",
    "ExportFailure",
    "Exporter",
    "ForwarderSettings",
    "Span",
    "activate",
    "describe_http_failure",
    "forwarder",
    "parse_headers",
    "span_from_dataset",
]

log = get_logger("mercury.otel")

# the engines run the forwarder with two workers
FORWARDER_INSTANCES = 2


def forwarder(exporter: Exporter) -> Callable[[dict[str, str], Any], Awaitable[None]]:
    """The ``distributed.trace.forwarder`` function bound to one exporter: map
    the dataset to a span and export it, logging - never raising - a failure."""

    async def forward(_headers: dict[str, str], body: Any) -> None:
        # a dataset without W3C-valid ids cannot become a span without forging ids - skipped
        span = span_from_dataset(body)
        if span is None:
            return
        try:
            await exporter.export(span)
        except ExportFailure as failure:
            log.warning("OTLP export failed for span %s of trace %s - %s",
                        span.span_id_hex, span.trace_id_hex, failure)

    return forward


def enabled(config: Any) -> bool:
    """Whether ``otel.forwarding`` is switched on (the text ``true``, case-insensitive)."""
    value = config.get_property(FORWARDING_SWITCH)
    return value is not None and value.strip().lower() == "true"


def activate(config: Any, registry: Any) -> Exporter | None:
    """Register the built-in forwarder on ``distributed.trace.forwarder`` when
    ``otel.forwarding=true`` - the host's start-up hook. Returns the exporter
    (so the host can close it at shutdown), or ``None`` when forwarding is off
    or a function already occupies the route (an application's own forwarder
    wins, as in the engines). A misconfigured endpoint fails the start."""
    if not enabled(config):
        return None
    if registry.exists(DISTRIBUTED_TRACE_FORWARDER):
        log.info("%s is provided by the application - the built-in OpenTelemetry forwarder "
                 "stands down", DISTRIBUTED_TRACE_FORWARDER)
        return None
    exporter = Exporter(ForwarderSettings.from_config(config))
    names = exporter.header_names()
    log.info("OpenTelemetry trace forwarder ready - service=%s, OTLP endpoint=%s, "
             "compression=%s, credential headers=%s", exporter.service_name, exporter.endpoint,
             exporter.compression, names)
    if not names:
        log.info("No OTLP credential header yet (%s unset) - it is re-read on every export, so "
                 "a credential published later takes effect without a restart", HEADERS)
    registry.register(DISTRIBUTED_TRACE_FORWARDER, forwarder(exporter),
                      instances=FORWARDER_INSTANCES, private=True)
    return exporter
