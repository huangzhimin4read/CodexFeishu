---
name: manage-codex-feishu
description: Operate, verify, deploy, restart, or diagnose the owner-only Windows bridge between Codex desktop tasks and Feishu/Lark topics. Use for CodexFeishu service health, Feishu-to-Codex delivery, Codex-to-Feishu mirroring, exact return-path deduplication, topic routing, attachments, receipts, remote commands, or scheduled-task recovery.
---

# Manage Codex Feishu / Lark

Operate the always-on CodexFeishu bridge conservatively and verify delivery from durable evidence. Keep the Windows scheduled service as the message transport; this skill is its management surface, not a replacement daemon.

## Locate the repository

Use, in order:

1. A repository path explicitly supplied by the user.
2. The current workspace when `pyproject.toml` declares `name = "codex-feishu-bridge"`.
3. `CODEX_FEISHU_REPOSITORY` when it resolves to that project.

Do not scan arbitrary drives. Treat `.runtime/live-remote.toml`, tenant exports, tokens, application secrets, chat IDs, open IDs, and task IDs as private. Never print or commit them.

## Start with read-only status

Run the bundled script before changing the installation:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/status.ps1 -RepositoryRoot <repo>
```

The bridge is healthy only when all of these are true:

- Scheduled task `CodexFeishu-Broker-Owner` is running.
- The recorded service PID is alive.
- `.runtime/topic-group-status.json` is fresh.
- `process_state` is `running`.
- `remote_connection_state` is `connected`.
- No circuit breaker is open and no dead letter is pending.

Report failed conditions individually. Do not expose identifiers or message bodies in status output.

## Verify the code

Before deployment, run from the repository:

```powershell
python -m pytest -q
python -m compileall -q codex_feishu_bridge
python -m codex_feishu_bridge verify-config --config .runtime/live-remote.toml
python -m codex_feishu_bridge preflight --config .runtime/live-remote.toml --live
```

If the private live config is absent, run the public test and compile checks and state that live preflight was not performed. Also scan tracked changes for credentials, real tenant identifiers, and local absolute paths in public documentation.

When owner-identity mirroring is requested, run the repository's guided prerequisite step:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_lark_cli.ps1 -Profile codex-feishu-owner
```

It must use the official `@larksuite/cli` package and guide the user through `config init --new`, `auth login --recommend`, and `auth status --json --verify`. Never collect or print the OAuth token. If the user skips this optional step, keep bot notifications available and leave owner-identity mirroring disabled.

## Deploy or restart

Only deploy or restart when the user requested repair, deployment, installation, or a change that requires activation.

1. Record the current task state, PID, health snapshot, and deployed code hashes.
2. Back up the installed application directory to a timestamped directory under `C:\ProgramData\CodexFeishuBridge\backups`.
3. Stop the exact scheduled task.
4. Stop only processes whose command line names the exact live config path. Never kill Python or Codex processes broadly.
5. Stage the locked dependencies and copy the bridge package and supervisor scripts.
6. Install or start the supervisor task.
7. Require a new supervisor PID, a new worker PID, fresh health, and a connected Feishu long connection.
8. Re-run `scripts/status.ps1` and relevant tests.

Preserve the private configuration and credentials. Do not delete backups automatically.

## Prove delivery end to end

Use a new unique message for each direction and report evidence, not just UI impressions.

For Feishu to Codex, require:

- A new ingress row for the Feishu message.
- An accepted dispatch record containing the exact target Codex task and turn ID.
- With `delivery = "cli"`, a new `task_started -> turn_context -> user message` sequence whose exact body and image wrappers match the ingress, plus a stable persisted user-item ID. Store that item ID on the accepted dispatch so the exact Feishu-origin user message is suppressed when rollout observation sees it again.
- With `delivery = "desktop_relay"`, require the configured private relay task to receive exactly one Desktop prompt and the target task to receive exactly one `<codex_delegation>` user item whose source task, decoded input, turn ID, and item ID match the dispatch record. The relay task must remain excluded from task bindings, topic creation, and Feishu mirroring.
- The same user text and images visible in the Codex task reader or rollout record.

For Codex to Feishu, require:

- A normalized Codex `user_message` item.
- Exactly one confirmed provider-outbox delivery for that item.
- No desktop-dispatch record for a Codex-origin user item.

When `user_message_identity = "lark_cli_user"`, require the configured CLI profile to verify as a ready user whose Open ID exactly equals `owner_open_id` before starting the service. Text should then appear as that Feishu user. Do not silently use a differently authorized CLI account. Permanent CLI authorization/configuration errors may fall back to a bot message labeled with `owner_display_name`; retryable or unknown results must remain queued to avoid duplicates.

On Windows, require user-identity delivery to invoke the official npm-installed `@larksuite/cli` JavaScript entrypoint through Node instead of the `lark-cli.cmd` shim. The batch shim lets `cmd.exe` reinterpret literal `<` and `>` in message text as redirection. Treat persisted `<subagent_notification>...</subagent_notification>` and standalone `<environment_context>...</environment_context>` user-role records as internal Codex context, never as owner-authored messages; retire already queued copies as terminal suppressed records without deleting their audit evidence.

Also verify that the Feishu event emitted by a successful user-identity send is classified as `outbound_echo` and never creates a desktop dispatch. Cover the callback-before-confirmation race with a short delayed reconciliation against the exact pending user-message body, task, and reply target. An independently authored later message with identical text must still route normally.

For a message injected from Feishu, verify the exact return item is suppressed by `task + turn + user item` identity. Do not deduplicate by body text: an independently authored identical message must still deliver.

## Preserve product boundaries

- Create or rename a Feishu project group only after that Codex project becomes active and no mapped group exists.
- Keep one Feishu topic per Codex task and use the visible task name plus project name; omit internal task IDs, hashes, citation XML, and local file paths.
- Distinguish task state from the persisted Codex phase, not message wording. Mirror `commentary` as ordinary process output; after every `final_answer`, keep all final text/images and send a separate `🔔【等待你的回应】` cue as the last topic message.
- Render links and file citations as path-free visible labels. Upload accessible local images/files instead of exposing their paths.
- Keep remote text, images, files, approvals, and control commands behind the configured owner-only authorization and audit policy.
- `delivery = "cli"` persists through `codex exec resume` and attaches local images with `--image`. Codex allows one writer per task: when another writer owns the task, retain the ingress in the durable queue and retry with CLI later. Never invoke or manipulate the Codex Desktop composer while `delivery = "cli"`. `delivery = "desktop_relay"` is an explicitly selected alternative: keep one non-minimized primary Codex window available for its configured internal relay task and use other Codex windows for normal work. Prefill through the local-task `codex://threads/<id>?prompt=...` deep link only; the verified pure deep-link second-instance branch must not call the normal `show/focus` path. Invoke only the selected renderer's Send control, never global keyboard/clipboard input or explicit foreground activation. Confirm the exact relay user item and exact delegated target item from new rollout bytes before acceptance; otherwise defer durably. Use `desktop` only for separately selected direct UI automation.
- Treat Feishu's hollow circle as native read-status UI. The normal Feishu API and `lark-cli` cannot mark an owner-authored incoming message read on behalf of Codex, so do not claim to clear it or synthesize a read receipt.
- Do not confuse content-level approval messages with Codex permission prompts. Actual prompts for desktop-owned turns follow that Codex task's permission profile.
- Do not claim delivery merely because Feishu accepted an event or the bridge accepted a queue item. Distinguish submitted to Codex, visible in Codex, sent to Feishu, and confirmed by Feishu.

## Public release checks

Before committing or publishing:

- Keep `.runtime/live-remote.toml`, databases, logs, screenshots, tenant exports, and credentials ignored.
- Maintain matching English and Chinese user documentation.
- Validate this skill and the plugin manifest with the bundled skill/plugin validators.
- Run `git diff --check`, the full test suite, and a tracked-content secret/identifier scan.
- Commit intentionally and verify the remote commit after pushing.
