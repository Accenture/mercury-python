"""
Mercury Composable — polyglot functions for Python.

A lightweight Event-over-HTTP function host and client: write decoupled
functions in Python and let Java/Rust Mercury engines orchestrate them from
Event Script flows and MiniGraph knowledge graphs through the declarative
``yaml.event.over.http`` routing map. Orchestration stays in the engines;
this package deliberately provides functions only, plus the minimalist
utilities (configuration, logging, telemetry) shared with the engine style.
"""

from .client import PostOffice
from .config import AppConfig, app_config, load_config
from .envelope import Body, EventEnvelope, iso_utc
from .event_stream import EventStreamWriter
from .exceptions import AppException, CompactFormatError
from .log import get_logger
from .registry import FunctionRegistry, Handler, default_registry, preload
from .server import EventApiServer, Platform, platform
from .trace import TraceInfo, annotate_trace, get_trace, trace_context, update_context
from .version import __version__

__all__ = [
    "AppConfig",
    "AppException",
    "Body",
    "CompactFormatError",
    "EventApiServer",
    "EventEnvelope",
    "EventStreamWriter",
    "FunctionRegistry",
    "Handler",
    "Platform",
    "PostOffice",
    "TraceInfo",
    "__version__",
    "annotate_trace",
    "app_config",
    "default_registry",
    "get_logger",
    "get_trace",
    "iso_utc",
    "load_config",
    "platform",
    "preload",
    "trace_context",
    "update_context",
]
