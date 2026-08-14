# Codex Feishu Bridge

[简体中文](README.zh-CN.md) | English

Codex Feishu Bridge is an owner-operated, Windows-local bridge between OpenAI Codex and Feishu (Lark). It mirrors Codex task output into private Feishu topic groups and can, behind explicit capability gates, route owner messages, images, files, approvals, and control commands back to the exact Codex task.

The project is designed around a fail-closed control plane. Unknown identities, chats, reply ancestry, task bindings, provider delivery results, executable versions, schemas, and approval outcomes are rejected or held for reconciliation instead of being guessed.

> **Alpha software:** this repository is suitable for development and a carefully controlled single-owner pilot. It is not a hosted service, a multi-user bot platform, or a production certification.

## Highlights

- **Project and task topics:** activity-triggered private groups for Codex projects, with one Feishu topic per Codex task.
- **Reliable outbound delivery:** durable SQLite outbox, stable provider UUIDs, retry classification, delivery reconciliation, dead letters, and circuit breakers.
- **Readable mobile output:** process/final messages, project-local Markdown images, visible Codex image outputs, file-citation labels, and link destinations hidden from provider-visible text.
- **Strict inbound routing:** owner, tenant, app, chat, topic root, ancestry, task epoch, project root, and capability binding are checked before dispatch.
- **Remote inputs:** independently gated text, image, and file input. An idle task uses `turn/start`; an exact active task uses `turn/steer` immediately instead of waiting for the current turn to finish. Files are bounded, hashed, stored under the selected project's inbox, and never auto-executed or auto-extracted.
- **Truthful submission status:** Feishu reports `submitted` only after Codex App Server accepts the request. Busy/ambiguous or unavailable states are reported as not submitted; no human-style “read” state is invented.
- **Approvals and controls:** short-lived single-use approval actions plus scoped status, task, profile, append, stop, and hard-stop commands.
- **Windows isolation:** provider credentials stay with the broker identity; Codex App Server work can run through a separate non-administrator worker identity with ACL and Job Object boundaries.
- **Version gates:** the Codex executable and generated stable/experimental App Server schemas are pinned and hashed.

## Architecture

```text
Codex Desktop state / rollout files
             |
             v
  Normalizer + binding checks -----> SQLite items/outbox/audit
                                             |
                                             v
                                  Feishu REST + WebSocket
                                             |
                                             v
                                   Private project topics

Feishu owner input
       |
       v
identity/chat/root/epoch/capability checks
       |
       v
separate Windows worker -> Codex App Server -> exact task
```

The bridge is deliberately local. There is no public webhook and no project-wide directory crawl. Project and task identity come from the configured Codex state, while every writable project root remains allowlisted.

## Requirements

- Windows 10/11 or Windows Server with PowerShell and Task Scheduler
- Python 3.11 or later
- An installed Codex CLI/App Server executable
- A Feishu custom app and bot with tenant-approved permissions for the features you enable
- Windows Credential Manager for the Feishu app secret
- A separate non-administrator Windows account before enabling remote control or approvals

Feishu scopes, callback subscriptions, rate limits, and response contracts can change. Treat the example contract as a template and validate it against the current developer-console export for your tenant.

## Quick start

```powershell
git clone https://github.com/huangzhimin4read/CodexFeishu.git
Set-Location CodexFeishu

py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[test]"

python -m pytest -q
python -m codex_feishu_bridge verify-config --config config/offline.example.toml
```

For a real local configuration:

1. Copy `config/runtime.example.toml` or `config/runtime.topic-group.example.toml` into the ignored `.runtime` directory.
2. Replace every `REPLACE_*` value and keep unapproved endpoints/capabilities disabled.
3. Export and review the tenant contract, using the corresponding `*.example.json` as a template.
4. Store the Feishu app secret as a Windows Generic Credential whose target matches `credential_target`. Never place the secret in TOML, JSON, logs, or evidence.
5. Generate a local Codex schema baseline for the exact executable you will run:

   ```powershell
   python scripts/generate_codex_baseline.py --codex-executable "C:\path\to\codex.exe"
   ```

6. Verify configuration and tenant preflight before starting:

   ```powershell
   python -m codex_feishu_bridge verify-config --config .runtime/runtime.toml
   python -m codex_feishu_bridge preflight --config .runtime/runtime.toml --live
   python -m codex_feishu_bridge run --config .runtime/runtime.toml
   ```

Remote text, images, files, approvals, and controls are separate booleans and remain disabled until explicitly configured. The same-account fallback for high-privilege App Server work is intentionally rejected.

## Repository layout

| Path | Purpose |
| --- | --- |
| `codex_feishu_bridge/` | Bridge runtime, protocol adapters, storage, security controls, and operations |
| `config/*.example.*` | Fail-closed configuration and tenant-contract templates |
| `generated/codex/` | Pinned Codex App Server schema fixtures and compatibility matrix |
| `scripts/` | Baseline generation, evidence, Windows isolation, and service helpers |
| `tests/` | Unit, protocol, routing, storage-fault, image, and service tests |
| `SECURITY.md` | Trust boundaries and vulnerability-reporting rules |

## Files that must stay private

The `.gitignore` intentionally excludes runtime databases, WAL files, logs, evidence, live TOML files, tenant-console exports, app-specific tenant contracts, and internal host records. Before publishing a fork, also scan commit history—not only the current working tree—for secrets and tenant identifiers.

## Development

```powershell
python -m pip install -e ".[test]"
python -m pytest -q
python -m compileall -q codex_feishu_bridge tests scripts
```

Protocol or Codex upgrades require regenerating both stable and experimental schemas, reviewing the compatibility matrix, rerunning the full suite, and treating the new executable hash as a new release gate.

See [CONTRIBUTING.md](CONTRIBUTING.md) before submitting changes. Security issues should follow [SECURITY.md](SECURITY.md) and must not include real prompts, message bodies, tokens, tenant exports, or approval payloads.

## License

[MIT](LICENSE)
