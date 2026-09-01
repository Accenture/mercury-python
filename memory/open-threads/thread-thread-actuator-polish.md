- [x] (feature — Eric's three loose ends 2026-08-24; **MERGED same day as
  [PR #19](https://github.com/Accenture/mercury-python/pull/19), true merge `035b636`
  carrying `da60593` (tree verified, branches deleted both ends); node twin merged in
  its quality PR #88**) **Actuator polish: engine-parity index page, pretty
  JSON, host error shape.** `GET /` = the engines' minimal Welcome page (embedded — no
  static file service by design); actuator JSON pretty-printed (SimpleMapper default);
  unknown paths/non-GET → `{"status", "message", "type": "error"}` with
  `Resource not found` (SimpleHttpUtility signature, Java insertion order). Live-proven
  byte-symmetric with node. Relates [[thread-actuator-endpoints]].
  <!-- id: thread-actuator-polish | created: 2026-08-24 | last_used: 2026-08-24 | uses: 1 | tier: active | origin: 2026-08-24-015208 -->
