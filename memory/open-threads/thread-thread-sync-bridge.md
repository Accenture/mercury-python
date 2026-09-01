- [x] (feature — Eric's directive 2026-08-24 after ratifying the sync-vs-async design;
  **MERGED same day as [PR #18](https://github.com/Accenture/mercury-python/pull/18),
  true merge `1888a48` carrying branch head `af039db` (4 commits: bridge + import hoist
  + static _run_sync + unshadow); tree verified identical, branches deleted both
  ends**) **Sync bridge: `PostOffice.request_sync()/send_sync()` from plain-def
  handlers.** The bus stamps the host loop into a contextvar before dispatching sync
  handlers; the bridge submits the same coroutines via `run_coroutine_threadsafe`,
  blocking only the worker thread. Durable subtlety: **contextvars do not cross
  run_coroutine_threadsafe** — the bridge re-establishes the caller's TraceInfo inside
  the submitted task (same object), keeping the trace chain unbroken. Teaching errors:
  on-loop call → "await request() instead"; off-host → use asyncio.run. Rationale docs
  (requests/NumPy named, virtual-threads analog) in README + registry.py per Eric.
  4 pins + hello.sync.chain wire proof. Relates [[thread-primitive-event-bus]].
  <!-- id: thread-sync-bridge | created: 2026-08-24 | last_used: 2026-08-24 | uses: 1 | tier: active | origin: 2026-08-24-004715 -->
