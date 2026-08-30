"""Application log context tests - the engines' app-log-context twin:
default template rendering, the update_context developer API, feature
gating and the structured-formatter integration."""

import json
import logging
from collections.abc import Iterator

import pytest

from mercury_composable import trace_context, update_context
from mercury_composable.config import app_config
from mercury_composable.log import EngineJsonFormatter
from mercury_composable.log_context import (
    LogContextConfig,
    default_template,
    log_context_config,
    reset_for_test,
)
from mercury_composable.trace import TraceInfo


@pytest.fixture(autouse=True)
def fresh_template() -> Iterator[None]:
    reset_for_test()
    yield
    app_config().set("app.log.context", "true")
    reset_for_test()


def info_under_test() -> TraceInfo:
    return TraceInfo(route="llm.chat", trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
                     trace_path="POST /api/agent/run", my_correlation_id="biz-7788",
                     span_id="82d8a6ccd03638fe", parent_span_id="00f067aa0ba902b7")


def test_default_template_renders_the_standard_trace_context():
    # the default ships as a packaged YAML resource, the engines' twin
    template = default_template()
    assert template is not None, "packaged default-log-context.yaml must load"
    context = LogContextConfig(template).render(info_under_test())
    assert context == {
        "cid": "biz-7788",
        "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
        "tracePath": "POST /api/agent/run",
        "spanId": "82d8a6ccd03638fe",
        "parentSpanId": "00f067aa0ba902b7",
        "service": "llm.chat",
        "timestamp": context["timestamp"],
    }
    # absent values are omitted, never rendered as "null" - and cid is the
    # BUSINESS correlation-id only
    bare = LogContextConfig(default_template()).render(
        TraceInfo(route="llm.chat", trace_id="t-1", cid="internal-routing-id"))
    assert "cid" not in bare
    assert "spanId" not in bare


def test_custom_template_tokens_constants_and_validation():
    config = LogContextConfig({"trace": "$traceId", "deployment": "blue"})
    context = config.render(info_under_test())
    assert context == {"trace": "4bf92f3577b34da6a3ce929d0e0e4736",
                       "deployment": "blue"}
    with pytest.raises(ValueError, match="Invalid log context token"):
        LogContextConfig({"x": "$bogus"})


def test_update_context_merges_and_guards_reserved_keys():
    config = LogContextConfig(default_template())
    with trace_context("trace-ctx-1", "TEST /ctx") as info:
        update_context("tenant", "acme")
        assert config.render(info)["tenant"] == "acme"
        update_context("tenant", None)
        assert "tenant" not in config.render(info)
        with pytest.raises(ValueError, match="reserved"):
            update_context("cid", "nope")
    # outside a hosted request: a silent no-op, the engines' semantics
    update_context("tenant", "ignored")


def test_structured_formatter_emits_the_context_block():
    record = logging.LogRecord(name="app", level=logging.INFO, pathname=__file__,
                               lineno=7, msg="charging the order", args=None,
                               exc_info=None)
    formatter = EngineJsonFormatter(compact=True)
    with trace_context("4bf92f3577b34da6a3ce929d0e0e4736", "POST /api/agent/run",
                       my_correlation_id="biz-7788"):
        entry = json.loads(formatter.format(record))
    assert entry["message"] == "charging the order"
    context = entry["context"]
    assert context["cid"] == "biz-7788"
    assert context["traceId"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert context["tracePath"] == "POST /api/agent/run"
    # outside a trace: no context block at all
    assert "context" not in json.loads(formatter.format(record))


def test_feature_flag_disables_the_context_block():
    app_config().set("app.log.context", "false")
    reset_for_test()
    assert log_context_config().enabled is False
    record = logging.LogRecord(name="app", level=logging.INFO, pathname=__file__,
                               lineno=7, msg="quiet", args=None, exc_info=None)
    with trace_context("trace-off-1", "TEST /off"):
        entry = json.loads(EngineJsonFormatter(compact=True).format(record))
    assert "context" not in entry
