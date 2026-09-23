- [ ] **(blueprint) Recurring interop gate on every release.** First PyPI publication is
  done (v4.12.1, 2026-09-01 — `pip install mercury-composable`; constrained sdist). The
  v4.12.0 progressive-rendering interop report was green once. Remaining Vision success
  criterion: live interop against both engines **on every release** — met at 4.12.15 by the
  four-runtime Dynatrace drive of 2026-09-22 (cited in the CHANGELOG intro); the package build/test
  workflow exists since 2026-09 (`ci.yml`). Recurring by nature — Eric decides whether it stays a
  Blueprint gap or becomes a release convention (refreshed 2026-09-23).
  Cadence and supply-chain posture stay Eric-gated (design P5/D6).
  → serves: vision-mercury-python
  <!-- id: bp-publish-interop-gate | created: 2026-08-22 | last_used: 2026-09-23 | uses: 8 | tier: working | origin: 2026-08-22-173136 -->
