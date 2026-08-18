# Contributing

Thank you for helping improve Codex Feishu Bridge.

## Development workflow

1. Create a focused branch.
2. Preserve stable-ID routing, exact de-duplication, durable queues, and private credential handling.
3. Add regression tests for every behavior change and fault path.
4. Run:

   ```powershell
   python -m pip install -e ".[test]"
   python -m pytest -q
   python -m compileall -q codex_feishu_bridge tests scripts
   ```

5. Explain the user-visible effect, trust-boundary impact, and validation in the pull request.

## Security and privacy

Never commit app secrets, access tokens, tenant-console exports, real chat/user/message identifiers, runtime databases, prompts, message bodies, approval payloads, audit keys, Windows SIDs, or local user paths. Use `REPLACE_*` values and synthetic fixtures.

Do not weaken identity, ancestry, binding-epoch, schema, executable-hash, delivery-reconciliation, or Windows-principal checks merely to make a test or deployment pass. If an external result is uncertain, preserve the uncertain state and require reconciliation.

Report vulnerabilities according to [SECURITY.md](SECURITY.md).
