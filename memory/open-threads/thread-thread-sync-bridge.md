- [x] Sync bridge: `PostOffice.request_sync()` / `send_sync()` from plain-def handlers.
  Outcome: [PR #18](https://github.com/Accenture/mercury-python/pull/18) merge `1888a48`
  (2026-08-24). Lesson: contextvars do not cross `run_coroutine_threadsafe` — re-stamp
  TraceInfo in the submitted task. origin: 2026-08-24-004715
  <!-- id: thread-sync-bridge | created: 2026-08-24 | last_used: 2026-08-24 | uses: 1 | tier: archive-candidate | origin: 2026-08-24-004715 -->
