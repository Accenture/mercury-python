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
  - **Build ordered 2026-11-25 (Eric: "agree with RFC-0018. Please proceed with implementation") and step 1 BUILT
    the same day, uncommitted at this note:** `rest_routing.py` (the parser with the engines' refusals and the pack's
    own, the matching, the transforms, the body dispatch, the negotiation, the response mapping),
    `async_http_request.py` (the request model, exported), `rest_automation.py` (the edge as an aiohttp middleware in
    front of the host's routes: the trace and correlation ids, authentication, the RPC and the interceptor sink, SSE
    and chunked streaming, the built-in default entries, the static-content filter and no-cache pages), `server.py`
    and `actuator.py` wired, the demo's `hello.rest`; 126 shared vector cases and 45 tests; the three gates clean;
    the demo driven live. **Raised as PR #83, 2026-11-25.**
  - **Defaults set without a ruling (open to the review):** `upload: true` refused at load rather than ignored; an
    unclaimed path answered by the edge with the engines' 404 in their header shape; `keep` takes precedence over
    `drop` (the Java rule, where the Rust port applies both); one `yaml.rest.automation` location, a missing or empty
    file degrading to the defaults with a warning, a relative path under the application folder; the service check
    at the end of the boot, after the extensions' startup registrations; an authentication function is a regular
    function; no reply-lane pool (no 503 on exhaustion); a filter failure serves the file (the Rust divergence); no
    `/index.html` entry leaves `/` to the static folder and the landing page.
  - **Next:** the pull request; the Node twin from the same vector file; the engines' polyglot guide and the RFC's
    resolution; a live drive through an engine's `async.http.request` with the trace lineage checked.
  - **Upload BUILT 2026-11-25 (Eric: "Please add upload feature after this PR"; the use case: documents uploaded
    to the LLM helper for a model to read), branch `feat/rest-upload` stacked on the step-1 branch:** `upload: true`
    on a POST entry is the pack's bounded form of the engines' streaming upload - the files of a multipart body in
    the request map under the engines' keys with their bytes in `contents`, together within
    `rest.upload.max.bytes` (16 MiB, 413 beyond it), the form's fields as query parameters; `AsyncHttpRequest.files()`
    and `UploadedFile` exported; the LLM helper's `llm.upload` behind `POST /api/llm/upload` saves the files under
    `uploads/` inside the attachments root under new names and returns the references (`path`, the kind by the
    first bytes, the size, the SHA-256, `attachable`). Vectors version 2 (129 cases), 50 REST tests and
    `tests/test_llm_upload.py`; the gates clean; driven live (a PDF and an image uploaded to the running helper,
    the saved bytes identical, the trace record left). **Raised as PR #84 against the step-1 branch, 2026-11-25.**
  - **Defaults set without a ruling (upload):** one bound for all the files of an upload, 16 MiB; the body absent on
    an upload (Java parity) and the bytes in the pack key `contents` in place of the stream routes; multipart on an
    entry without `upload` stays raw bytes; the intake's saved name `<id>-<the original name made safe>`, never
    overwritten; `attachable` by the first bytes; the helper turns REST automation on in its own application.yml.
  → proposal: RFC-0018 (mercury-composable)
  → serves: vision-mercury-python
  <!-- id: bp-rest-automation-python | created: 2026-11-24 | last_used: 2026-11-25 | uses: 3 | tier: working | origin: 2026-11-24-233943 -->
