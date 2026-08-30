# Event Streaming

A function that produces its result progressively — an LLM relay emitting tokens, a
long-running job reporting progress — should not make its caller wait for the whole
answer. This chapter is the wrapper's half of the platform-wide streaming contract:
the same paradigm on all four runtimes (Java, Rust, Python, Node.js):

> **The caller provides a reply address; the callee streams events to it until a
> terminal signal.**

Each segment is one event to the caller's `reply_to`, marked with the reserved
envelope header `x-event-stream: data | eof | exception`. A calling engine renders
the segments out its HTTP edge, hands them to a flow, or relays them onward — your
Python function neither knows nor cares.

## Write a streaming function

A streaming producer is an **interceptor**: it receives the raw `EventEnvelope`
(so the caller's reply address travels the engines' way) and replies through
`EventStreamWriter` instead of a return value:

```python
from mercury_composable import EventEnvelope, EventStreamWriter, preload

@preload(route="hello.tokens", instances=10, interceptor=True)
async def stream_tokens(headers: dict[str, str], event: EventEnvelope):
    out = EventStreamWriter.from_request(event)
    out.first(200, "text/event-stream")      # head control rides the first event
    out.write("The answer is")               # data segment
    out.write_named("tokens", {"n": 2})      # named (typed) SSE event
    out.close({"usage": {"tokens": 2}})      # end of transmission + trailing metadata
    # or out.fail(e)                         # in-band failure
```

The writer is the engines' exact API. `first(status, content_type, ttl_seconds=None)`
declares the response head and, optionally, the idle allowance between segments;
`fail(e)` carries the standard error key-values
`'{"type": "error", "status": n, "message": text}'`; writes after `close()`/`fail()`
are dropped. Plain-`def` handlers can stream too — the writer bridges from the
executor thread back to the host loop.

An interceptor's return value is never auto-replied. To answer single-shot from an
interceptor (a relay that sometimes buffers, for example), send a plain envelope to
`event.reply_to` yourself. An uncaught exception becomes the standard error envelope
to the caller — single-shot before the stream starts, in-band after.

## How it crosses the wire

When a calling engine (or `curl`) invokes your streaming function through
`POST /api/event` with `Accept: text/event-stream`, the host answers the same call
with a Server-Sent Events response in the platform's hybrid dialect:

- **envelope frames** — the reserved SSE event name `envelope`, one base64-encoded
  serialized envelope per frame — carry everything with envelope semantics: the
  first event (head control), the `eof`/`exception` terminals, and any segment that
  cannot round-trip as plain text (a dict or bytes body, text containing a carriage
  return, an event name colliding with the reserved word);
- **raw SSE frames** carry plain text segments, so token relays stay near-zero
  overhead.

Everything degrades explicitly: a caller that did not opt in receives
`406 Streaming function requires a caller that accepts text/event-stream` instead of
a truncated reply; a non-streaming (single-shot) answer over the capable path is
byte-identical to a normal RPC reply; idle expiry fails the stream in-band with the
standard 408 error body. The `x-ttl` request header (ms) is the idle allowance
between segments — your `first(..., ttl_seconds=...)` can extend it for the whole
stream. While the producer is quiet, the host emits `: ping` keep-alive comments
(`event.stream.keep.alive`, the engines' config key — default 30s, `0` disables).

## Consume a stream

`PostOffice.stream()` is the consumer surface — an async iterator yielding the same
decoded envelopes an engine reply route receives: `data` segments, then the terminal.
It works against a remote peer's `/api/event` (an engine or another function host)
and against local functions alike, and opting in is always safe — a non-streaming
target simply yields its one classic reply:

```python
from mercury_composable import PostOffice

async with PostOffice() as po:
    async for segment in po.stream("hello.tokens", None,
                                   endpoint="http://127.0.0.1:8100/api/event",
                                   timeout_ms=30000):
        marker = segment.headers.get("x-event-stream")
        if marker == "data":
            print(segment.body)
        elif marker == "exception":
            raise RuntimeError(segment.body["message"])
        # eof: segment.body carries the trailing metadata, if any
```

`timeout_ms` is the idle allowance between segments. The consumer guards the dialect
for you: a malformed frame, a stream that ends without a terminal, or idle expiry
each yield the standard in-band exception envelope, then the iterator ends.

## Compose a relay

The pattern the whole streaming program is built on: forward **your own caller's**
reply address into a call against a remote streaming function, and the segments flow
`engine → your function → remote peer → back to the original caller` with no
buffering anywhere:

```python
@preload(route="llm.relay", instances=10, interceptor=True)
async def relay(headers: dict[str, str], event: EventEnvelope):
    async with PostOffice() as po:
        await po.stream_to("remote.tokens", None,
                           reply_to=event.reply_to or "",
                           endpoint="http://peer:8085/api/event",
                           cid=event.cid, timeout_ms=30000)
```

`stream_to()` forwards every decoded envelope verbatim to the named LOCAL route
(here, the reply sink the host opened for your caller) and returns the terminal.
Combined with a calling engine's `stream: true` endpoint, this streams a remote
peer's tokens progressively out that engine's HTTP edge — with zero imperative
streaming code in between.

## See also

- The engines' HTTP Response Streaming guides (the same contract at the HTTP edge):
  [Java](https://accenture.github.io/mercury-composable/guides/http-streaming/) ·
  [Rust](https://accenture.github.io/mercury/guides/http-streaming/)
- [Interop Test Report — Progressive Rendering](../test-reports/progressive-rendering-interop.md) —
  the live four-runtime validation of this contract
- [HTTP Surface Reference](http-surface-reference.md) — the `/api/event` contract
- [Function Writing Patterns](function-patterns.md)
