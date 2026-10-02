- [ ] **AWS Bedrock through IAM as the LLM helper's second backend.** The helper's `Backend` seam is documented, not built:
  build the SDK's Bedrock client in `get_backend()` (it signs with the default AWS credential chain and takes a region, so
  there is no API key), give the `Backend` a `provider_model` that prefixes the model id (`anthropic.claude-opus-5-5`), report
  `supports_fallbacks=False` (server-side fallbacks are not offered on Bedrock), describe a missing AWS credential in
  `credential_problem()`, and select it with `llm.backend: 'bedrock'`. The routes, the contract and the vector file stay as they
  are, and the Node twin does the same in `defaultBackend()`. Needs Eric's AWS account for the live drive, with a negative
  control first (a bad credential must fail by name).
  → serves: vision-mercury-python
  <!-- id: llm-helper-bedrock-iam | created: 2026-10-01 | last_used: 2026-10-01 | uses: 1 | tier: working | origin: 2026-10-02-001146 -->
