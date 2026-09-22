- [ ] **OpenTelemetry forwarder lock-step and the v4.12.15 milestone (Eric's plan, 2026-09-22).** This host's
  forwarder is MERGED (PR #33, `697f5df4`, 2026-09-22). The Node twin (#101, #102) and the Rust playground E0 twin (mercury #314, open) landed; the four-runtime Dynatrace drive ran 2026-09-22 (this host's traces `c90af9e3…`, `a9686f1f…`; report MERGED, PR #35 `4083c47f`). Remaining: Eric's Dynatrace confirmation, then v4.12.15 on all four repos (this package jumps from 4.12.1, adopting the Java number). Fix on the way: the demo's default Gemini model id no longer resolves on Eric's key
  (`gemini-flash-latest` does). origin: 2026-09-22-164128
  <!-- id: otel-forwarder-lockstep | created: 2026-09-22 | last_used: 2026-09-22 | uses: 1 | tier: working | origin: 2026-09-22-164128 -->
