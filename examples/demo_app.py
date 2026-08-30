"""
Demo polyglot functions.

Run:  mercury-serve examples/demo_app.py

Configuration comes from examples/resources/application.yml (the engines'
"resources" convention - port 8086, the demo.health dependency, log format);
override any key with -Dkey=value, e.g. -Drest.server.port=8090.

Then map a route from a Mercury engine application (event-over-http.yaml):

    event.http:
      - route: 'hello.python'
        target: 'http://127.0.0.1:8086/api/event'
"""

import asyncio

from mercury_composable import (
    AppException,
    Body,
    EventEnvelope,
    EventStreamWriter,
    PostOffice,
    annotate_trace,
    get_logger,
    get_trace,
    platform,
    preload,
)

log = get_logger(__name__)


@preload(route="hello.python", instances=10)
def handle_event(_headers: dict[str, str], body: Body):
    """Uppercase transform - the polyglot hello world.

    The (headers, body) two-part signature is the function contract (the
    TypedLambdaFunction mirror) - a handler that does not need headers keeps
    the parameter, underscore-prefixed per Python convention.
    """
    if not isinstance(body, dict) or "text" not in body:
        raise AppException(400, "missing 'text'")
    annotate_trace("language", "python")
    log.info("Transforming text of length %d", len(str(body["text"])))
    return {"text": str(body["text"]).upper(), "language": "python"}


@preload(route="hello.declarative", instances=10)
async def declarative_echo(headers: dict[str, str], body: Body):
    """Echo for the composable-example declarative Event-over-HTTP demo."""
    return {"body": body, "headers": headers, "language": "python"}


@preload(route="demo.suffix.helper", instances=10, private=True)
async def suffix_helper(_headers: dict[str, str], body: Body):
    """Private helper - callable in-app only (the HTTP host answers 403 for it)."""
    assert isinstance(body, dict)
    return {"text": f"{body.get('text', '')}!", "language": "python"}


@preload(route="hello.chain", instances=10)
async def chain(_headers: dict[str, str], body: Body):
    """Local composition: a public function calls a private sibling through the bus."""
    reply = await PostOffice().request("demo.suffix.helper", body=body, timeout_ms=5000)
    return reply.body


@preload(route="hello.sync.chain", instances=10)
def sync_chain(_headers: dict[str, str], body: Body):
    """Sync composition: a plain-def handler (the requests/NumPy world) calls a
    sibling through the sync bridge - blocking its own worker thread only,
    never the event loop."""
    reply = PostOffice().request_sync("demo.suffix.helper", body=body, timeout_ms=5000)
    return reply.body


@preload(route="hello.tokens", instances=10, interceptor=True)
async def stream_tokens(headers: dict[str, str], event: EventEnvelope):
    """Streaming demo: paced test messages over the multi-shot reply contract.

    A calling engine consumes this progressively through Event-over-HTTP
    (accept: text/event-stream on the outbound event) and can render it out
    its own HTTP edge - engine-to-wrapper token streaming. Optional headers:
    "delay" ms between messages (default 500, clamped 50-5000) and "count"
    messages (default 5, clamped 1-100).
    """
    delay = min(5000, max(50, int(headers.get("delay", "500") or 500))) / 1000
    count = min(100, max(1, int(headers.get("count", "5") or 5)))
    # with log.format=json/compact, this line carries the application log
    # "context" block (trace ids, business cid) - see the streaming guide
    log.info("Streaming %d messages", count)
    out = EventStreamWriter.from_request(event)
    out.first(200, "text/event-stream")
    out.write("The following messages are rendered slowly to demonstrate streaming:")
    for n in range(1, count + 1):
        await asyncio.sleep(delay)
        out.write(f"test message {n} (python)")
    # the trailing metadata echoes the distributed trace id and the business
    # correlation-id, so a calling engine's edge shows both continuity
    # dimensions end to end
    info = get_trace()
    out.close({"count": count, "language": "python",
               "trace_id": info.trace_id if info else None,
               "my_correlation_id": headers.get("my_correlation_id")})


@preload(route="demo.health", instances=5, private=True)
async def health_check(headers: dict[str, str], _body: Body):
    """Health check speaking the engines' interface contract (type=info / type=health).

    Activated for the /health actuator endpoint by mandatory.health.dependencies
    in examples/resources/application.yml (or a -D override).
    """
    if headers.get("type") == "info":
        return {"service": "demo.service", "href": "http://127.0.0.1"}
    return "demo.service is running fine"


if __name__ == "__main__":
    platform.run()
