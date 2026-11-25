- [ ] **(blueprint) REST automation in the pack: `rest.yaml` as the router of a Python application, so a field utility app
  is built and deployed as an engine application is (RFC-0018 in mercury-composable, raised as its PR #674 and MERGED 2026-11-25; the
  Vision amended by Eric, 2026-11-24).** Investigated 2026-11-24 (log 2026-11-24-233943): viable with no new dependency - the host already has
  the one server, the Event API edge with its trace records, the actuators, static content, the mounted routes, the
  bus with per-call timeouts and the streaming sink; REST automation is a routing table and a request model in front
  of them, ingress and not orchestration. The proposed shape: `rest.automation: true` (off by default),
  `resources/rest.yaml` under the resources rule, the host's paths as the default entries, the Rust port's subset for
  version 1, `flow`, the HTTP relay and the dual service refused by name, `upload` and the content-type extras
  deferred; proof by a shared `rest-automation-vectors.json` with the Node twin, the engines' `rest-automation.json`
  loaded as a fixture and the Rust port's 33 tests as the checklist. Build order when the shape is ruled: the vector
  file, this pack, the Node twin, the README and the engines' polyglot guide, a live drive through a browser and an
  engine with the trace lineage checked. First adopters afterwards: the hand-mounted routes of the agent-question app
  and the ask-person flow.
  → proposal: RFC-0018 (mercury-composable)
  → serves: vision-mercury-python
  <!-- id: bp-rest-automation-python | created: 2026-11-24 | last_used: 2026-11-24 | uses: 1 | tier: working | origin: 2026-11-24-233943 -->
