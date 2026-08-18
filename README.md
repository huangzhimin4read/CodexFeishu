# Codex Feishu / Lark Bridge

[简体中文](README.zh-CN.md) | English

Codex Feishu / Lark Bridge is a single-user, Windows-local bridge between OpenAI Codex and Feishu (Lark). It mirrors Codex task output into private Feishu/Lark topic groups and routes text, images, files, approvals, and control commands from mapped topics back to the exact Codex task.

The public project exposes only this relaxed local mode and runs under the current Windows user. Stable tenant, app, project, task, chat, message, and reply IDs determine routing; bot, project, task, group, and user display names may change without stopping the service.

> **Alpha software:** this repository is intended for one trusted user on one Windows machine. It is not a hosted or multi-user bot platform.

## Highlights

- **Project and task topics:** activity-triggered private groups for Codex projects, with one Feishu topic per Codex task.
- **Task lifecycle and title sync:** renaming a Codex task updates the existing Feishu/Lark topic root in place (`task name|project name`) instead of creating a duplicate topic while Feishu still permits that root to be edited. Archiving a Codex task immediately disables its inbound/outbound bridge grant and, when editable, marks that same root `【已归档】`; activating the task again restores the live binding. A provider edit-window or edit-count rejection is persisted as a blocked title projection and is not retried in a loop.
- **Reliable outbound delivery:** durable SQLite outbox, stable provider UUIDs, retry classification, delivery reconciliation, dead letters, and circuit breakers.
- **Bidirectional message mirroring:** Codex user text, images, and path-free file labels are mirrored to the matching topic. The official `lark-cli` may send text as whichever authorized user profile is currently ready; startup does not compare that profile with a configured Open ID. Provider-message ancestry and durable `thread + turn + item` identity prevent the resulting callback and Feishu-origin item from looping or appearing twice.
- **Readable mobile output:** process/final messages, project-local Markdown images, visible Codex image outputs, file-citation labels, and link destinations hidden from provider-visible text.
- **Obvious handoff state:** commentary remains unobtrusive while Codex keeps running; every final answer ends with a separate `🔔【等待你的回应】` cue so an idle task is unmistakable in the Feishu/Lark topic and mobile preview.
- **Stable-ID inbound routing:** any human user in a mapped private topic can submit input. Tenant, app, chat, topic root, reply ancestry, and task ID route it; mutable names and the sender's Open ID are not authorization gates.
- **Remote inputs:** independently gated text, image, and file input. The recommended `cli` mode uses only Codex CLI. If another writer already owns the task, the message remains durably queued for a later CLI retry; this mode never manipulates the Codex Desktop composer. Files are bounded, hashed, stored under the selected project's inbox, and never auto-executed or auto-extracted.
- **Truthful submission status:** Feishu/Lark reports `submitted` only after the exact Codex user turn is confirmed. If neither writer can be verified, the message remains queued or unconfirmed instead of being claimed as delivered. Feishu's hollow read-status circle is native client UI and cannot be cleared by the bridge or the normal Feishu API.
- **Exact de-duplication:** source de-duplication never depends on equal message bodies. Rollout item identity, dispatch records, provider outbox identity, and Feishu UUIDs preserve at-most-once visible delivery across retries and restarts.
- **Approvals and controls:** short-lived single-use approval actions plus scoped status, task, profile, append, stop, and hard-stop commands.
- **One local process identity:** the bridge and Codex writer run under the current Windows user. `dangerFullAccess`, network access, and `approval_policy = "never"` are supported without a separate worker account.

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

Feishu/Lark topic input
       |
       v
tenant/app/chat/root/task-ID routing
       |
       v
Codex CLI resume -> persisted user turn -> exact task
```

The bridge is deliberately local. There is no public webhook. Project, task, chat, and message IDs are authoritative; display names and project paths are refreshed as mutable metadata. The allowlist only defines routing scope.

## Requirements

- Windows 10/11 or Windows Server with PowerShell and Task Scheduler
- Python 3.11 or later
- An installed Codex CLI/App Server executable
- For user-identity mirroring: the official `lark-cli`, with any ready authorized user profile; the installer guide detects it and walks through installation, configuration, login, and verification
- A Feishu custom app and bot with tenant-approved permissions for the features you enable
- Windows Credential Manager for the Feishu app secret
- An interactive Codex desktop session for `delivery = "desktop"` or `delivery = "desktop_relay"`

Feishu scopes, callback subscriptions, rate limits, and response contracts can change. Treat the example contract as a template and validate it against the current developer-console export for your tenant.

Feishu/Lark currently exposes no supported bot or official `lark-cli` operation for subscribing or unsubscribing one user from one topic. A user becomes subscribed through an actual user-identity reply in that topic. After confirming a new topic root, the bridge therefore sends one visible `🔔 已订阅任务更新` reply through the ready `lark-cli` profile and de-duplicates its callback before Codex dispatch. On Codex archive the bridge disables traffic and marks the root archived; removing the topic from the Feishu subscription list still requires **Cancel Subscription** in the client.

Feishu also limits message editing to an administrator-defined time window and at most 20 edits per message. Because a topic title is the root message, an older or frequently renamed topic may no longer be renameable through the supported API. The task binding and archive grant still change correctly; the bridge records the provider rejection instead of creating a duplicate replacement topic.

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
2. Replace every `REPLACE_*` value. The topic-group example enables the relaxed local feature set; turn off individual features only when you do not need them.
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

Remote text, images, files, approvals, and controls are independent feature switches; the relaxed topic-group example enables all five and automatic approvals. `delivery = "cli"` uses `codex exec resume`; a writer lock leaves ingress in the durable queue. `delivery = "desktop_relay"` sends input to one dedicated relay task identified only by `desktop_relay_thread_id`; renaming that task does not affect routing. The bridge prefills that exact task through the local-task prompt deep link and identifies its composer by the unique prompt text, without global keyboard, clipboard, or foreground activation. It acknowledges dispatch only after the relay and target rollout items are both present. `desktop` remains the direct UI-automation alternative, and `app_server` now runs under the same local Windows user.

## Repository layout

| Path | Purpose |
| --- | --- |
| `codex_feishu_bridge/` | Bridge runtime, protocol adapters, storage, security controls, and operations |
| `config/*.example.*` | Relaxed single-user configuration and tenant-contract templates |
| `generated/codex/` | Pinned Codex App Server schema fixtures and compatibility matrix |
| `plugins/codex-feishu/` | Optional Codex plugin for status, verification, deployment, and diagnosis workflows |
| `scripts/` | Baseline generation, evidence, installation, and service helpers |
| `tests/` | Unit, protocol, routing, storage-fault, image, and service tests |
| `SECURITY.md` | Trust boundaries and vulnerability-reporting rules |

## Codex plugin

The repository is also a public Codex plugin marketplace. Its optional `codex-feishu` plugin gives Codex a validated management skill and a path-safe, read-only health check for this bridge; the Windows scheduled service remains the always-on transport.

Give Codex this one-line request:

```text
Add the plugin marketplace from GitHub repository huangzhimin4read/CodexFeishu, install and enable the codex-feishu plugin, verify its status, and report the result.
```

Or install it directly from PowerShell:

```powershell
codex plugin marketplace add huangzhimin4read/CodexFeishu --ref main; if ($LASTEXITCODE -eq 0) { codex plugin add codex-feishu@codex-feishu }
```

Start a new Codex session after installation so the plugin is loaded. Installing the plugin adds the Codex management workflow; the bridge service itself still requires the repository setup and private Feishu credentials described below.

The broker installer runs the Feishu CLI prerequisite guide by default (use `-SkipLarkCliSetup` only when owner-identity mirroring is intentionally disabled). The same guide can also be run directly:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_lark_cli.ps1 -Profile codex-feishu-owner
```

It installs only the official `@larksuite/cli` package and runs the guided `config init --new`, `auth login --recommend`, and `auth status --json --verify` flow. OAuth tokens remain under the CLI's local credential management and are never written to this repository. Skipping the step keeps bot notifications available but disables owner-identity mirroring.

The plugin deliberately contains no credentials, tenant IDs, live configuration, or runtime database.

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
