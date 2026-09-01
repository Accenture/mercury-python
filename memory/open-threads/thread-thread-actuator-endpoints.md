- [x] (feature — Eric's directive, IMPLEMENTED + **MERGED 2026-08-23 in the same
  [PR #17](https://github.com/Accenture/mercury-python/pull/17) as the bus**)
  **Actuator endpoints — the engines' operational surface for Kubernetes PODs.**
  /info, /info/routes, /env, /health, /livenessprobe; health check functions speak the
  engines' `type=info`/`type=health` contract through the bus; UP 200 / DOWN 400;
  liveness follows the last health outcome. Durable lesson: engine `log.format` json =
  PRETTY-printed, compact = single-line JSONL (the JsonAppender/CompactAppender pair).
  Detail: origin log. Relates [[thread-primitive-event-bus]].
  <!-- id: thread-actuator-endpoints | created: 2026-08-23 | last_used: 2026-08-24 | uses: 2 | tier: active | origin: 2026-08-23-031558 -->
