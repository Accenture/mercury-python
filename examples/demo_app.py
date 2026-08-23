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

from mercury_composable import (
    AppException,
    Body,
    annotate_trace,
    get_logger,
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
    from mercury_composable import PostOffice

    reply = await PostOffice().request("demo.suffix.helper", body=body, timeout_ms=5000)
    return reply.body


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
