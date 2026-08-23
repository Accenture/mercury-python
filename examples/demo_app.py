"""
Demo polyglot functions.

Run:  mercury-serve examples/demo_app.py --port 8086

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


if __name__ == "__main__":
    platform.run()
