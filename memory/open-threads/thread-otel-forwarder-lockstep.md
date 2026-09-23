- [x] **OpenTelemetry forwarder lock-step and the v4.12.15 milestone — CLOSED 2026-09-23: v4.12.15 PUBLISHED.** PR #37 merge
  `95101575`, tag `v4.12.15` → `7bf6991`, GitHub release 01:37Z, PyPI 02:34Z (wheel + constrained sdist). Ships the forwarder
  (#33), the Gemini stable alias (#34), the four-runtime certification report (#35) and the span-kind rule (#36) — certified in
  the 2026-09-22 Dynatrace drive and confirmed in the UI; one number on all four runtimes (crates.io 12/12 01:57Z, npm
  2026-09-23 02:34Z). Lesson: the LLM provider, not the pipeline, decides which calls succeed — probe and pin the model per drive.
  origin: 2026-09-22-164128, 2026-09-22-194827; close 2026-09-23-004552.
  <!-- id: otel-forwarder-lockstep | created: 2026-09-22 | last_used: 2026-09-23 | uses: 3 | tier: active | origin: 2026-09-22-164128 -->
