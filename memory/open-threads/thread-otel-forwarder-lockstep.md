- [ ] **OpenTelemetry forwarder lock-step and the v4.12.15 milestone (Eric's plan, 2026-09-22).** This host's
  forwarder is MERGED (PR #33, `697f5df4`, 2026-09-22). Remaining: the mercury-nodejs twin (+ a Node
  `llm.stream` node), the Rust playground's E0 twin, then the four-runtime Dynatrace certification — Java and Rust
  edges rendering Gemini tokens progressively through this host and the Node host, one trace per request, Eric
  confirming in the Dynatrace UI — then v4.12.15 on all four repos (this package jumps from 4.12.1, adopting the
  Java number). Fix on the way: the demo's default Gemini model id no longer resolves on Eric's key
  (`gemini-flash-latest` does). origin: 2026-09-22-164128
  <!-- id: otel-forwarder-lockstep | created: 2026-09-22 | last_used: 2026-09-22 | uses: 1 | tier: working | origin: 2026-09-22-164128 -->
