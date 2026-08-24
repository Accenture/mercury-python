# Changelog

## 0.1.0 (unreleased)

- Sync bridge: `PostOffice.request_sync()` / `send_sync()` let a plain `def` handler
  (the synchronous ecosystem - `requests`, NumPy/ML inference, database drivers) call
  sibling or remote functions - the call runs on the host event loop while only the
  handler's worker thread blocks; the trace chain rides across unbroken. Calling the
  bridge from async code, or outside a hosted function, is refused with a teaching
  error. The sync-vs-async handler rationale is now documented (README and the
  registry module).
- Actuator endpoints `/info`, `/info/routes`, `/env`, `/health` and `/livenessprobe` -
  the engines' operational surface, for Kubernetes probes and one-dashboard monitoring
  of polyglot installations. Health check functions are normal registered functions
  speaking the engines' `type=info` / `type=health` interface contract, listed in
  `mandatory.health.dependencies` / `optional.health.dependencies` and called through
  the event bus. `/health` answers `UP` (200) / `DOWN` (400); `/livenessprobe` follows
  the most recent health outcome.
- `log.format` carries the engines' three presentations: `text` (default), `json`
  (pretty-printed) and `compact` (single-line JSONL for log aggregators). A sample
  `examples/resources/application.yml` demonstrates the resources convention and the
  well-known keys.
- Primitive in-process event bus - the single dispatch pipeline: one FIFO mailbox per
  route consumed by `instances` worker tasks (the parameter is faithful); RPC deliveries
  are ttl-bounded with a dead-work skip; drop-n-forget returns the 202-shape ack. The
  HTTP host and the local side of PostOffice are thin ingress adapters over it. No spill
  tier and no queue cap by design - back-pressure belongs to the engines' flows/graphs;
  a leaf host fails fast by deadline.
- PostOffice without an endpoint delivers locally (engine semantics): private routes are
  callable in-app while the wire keeps its 403; headers pass verbatim; the reply envelope
  shape is identical to the remote path.

Repository repurposed for the Mercury Composable **polyglot initiative** (August 2026).

- Lightweight Event-over-HTTP function host (`POST /api/event`) mirroring the engines'
  EventApiService semantics (x-ttl execution bound, x-async drop-n-forget with 202 ack,
  reserved header hygiene, `my_cid` → `my_correlation_id` injection, portable error
  contract, engine-identical error messages).
- Standard event envelope wire format codec, verified against the golden conformance
  vectors shared with the Java and Rust engines; compact format detected and rejected.
- `@preload` function registry with instance-count concurrency limits and private routes.
- `PostOffice` thin client with the engines' relay HTTP contract (octet-stream, x-ttl,
  x-no-stream, trace headers) for calling engine or peer polyglot functions.
- Minimalist utilities in engine-consistent style: `AppConfig` (`${ENV:default}`
  substitution, runtime overrides), logging in the reference log4j2 presentation
  (`log.format=text|json`, `LOG_LEVEL`), and distributed-trace context with reply
  annotations.
- `mercury-serve` developer runner.

The legacy Mercury language-pack implementation (pre-composable, websocket-based) remains
available in the git history prior to this version.
