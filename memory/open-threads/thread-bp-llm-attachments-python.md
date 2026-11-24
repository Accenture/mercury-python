- [ ] **(blueprint) Attachments on a single `llm.chat` call in the LLM helper: RFC-0016 build plan step 1, Python
  first, the Node twin in lockstep (the maintainer, 2026-11-24: "proceed with implementation for RFC 0015 and 0016";
  a field request, issue #78).** **Step 1 BUILT 2026-11-24 as PR #82 (merge `c367951`, MERGED 2026-11-24), on top of
  PR #81 of [[bp-llm-single-call-audit-python]]:** `attachments` (a path, or `{path, pages}`) names files inside
  `llm.attachments.root`; the helper reads them off the event loop and inside the call's deadline, decides the kind by
  the first bytes and sends base64 `image` and `document` blocks before the last user turn's text; `pages` cuts a PDF
  with pypdf (the `documents` extra); the audit of a single call keeps a reference with the source file's size and
  SHA-256 (`attach`, `_load_attachments`, `_attachment_file`, `_pdf`, `attachments_boot`).
  - **The maintainer's direction (2026-11-24):** the shared folder is configured in the helper and files are put
    there before a graph runs; a later iteration lets the helper download from an object store into the folder, for
    PDFs, images, spreadsheets and other inputs, and until then an external script does the download. Clarified in
    the same exchange: the helper reads the file and sends its bytes - the model reads nothing on the host, and the
    path never reaches it. A spreadsheet is not a kind the provider's blocks take, so that iteration needs a
    conversion step. On the audit, agreed the same day: the record keeps a reference and the hash, never the file,
    so an installation that must reconstruct an exchange keeps its files unchanged for the audit's retention.
  - **Proof:** `tests/vectors/llm-attachments-vectors.json` (new, 48 cases) over eight fixture files in
    `tests/fixtures/attachments`, and six tests in `tests/test_llm_attachments.py`; thirty-one mutations each caught.
    One mutation was first missed: a whole PDF rewritten by the library equalled the fixture the same library had
    written, so the fixture gained a title a rewrite does not carry.
  - **Defaults set without a ruling (RFC-0016's open questions):** one root; the byte bound on source files' sizes;
    no page limit of the helper's own; not on `llm.stream`; the `documents` extra; a missing root as a 503 and a root
    that is not a folder as a stop at start; hidden entries refused with no switch; an image's limit taken as 10 and
    5 times 1,048,576 bytes base64-encoded.
  - **Not done:** the measurement of a PDF cut beside a running stream; a live call (no token was spent).
  - **The Node twin MERGED 2026-11-24** (mercury-nodejs #145), from the same vector file and fixtures with no case
    or message changed; its PDF library is `@cantoo/pdf-lib`, the maintainer's choice, and a cut is asserted by its
    pages, not its bytes. RFC-0016's entry records both steps (mercury-composable #669 and #670).
  - **Next:** the schema page in mercury-composable; a live drive on both backends.
  → proposal: RFC-0016 (mercury-composable)
  → serves: vision-mercury-python
  <!-- id: bp-llm-attachments-python | created: 2026-11-24 | last_used: 2026-11-24 | uses: 1 | tier: working | origin: 2026-11-24-055310 -->
