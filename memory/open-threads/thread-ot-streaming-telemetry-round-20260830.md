- [x] (feature — **MERGED 2026-08-30 as
  [PR #21](https://github.com/Accenture/mercury-python/pull/21) true merge `bfca7e4`
  carrying `d50986a`; tree verified; v4.12.0 milestone, all four repos lock-step)
  **The progressive-rendering round: event streaming (engines' envelope-mode SSE
  contract, reply_to bus mechanism, stream/stream_to consumers), business
  correlation-id continuity, full span lineage with the engines' distributed-trace
  dataset on stdout, app-log-context with the packaged default template, sender
  attribution.** Lessons: detach long-lived workers from the creating task's
  contextvars; install the log-context config before its own warning logs; RPC legs
  emit no dataset (engine parity). origin: 2026-08-30-045556.
  <!-- id: ot-streaming-telemetry-round-20260830 | created: 2026-08-30 | last_used: 2026-09-01 | uses: 3 | tier: active | origin: 2026-08-30-045556 -->
