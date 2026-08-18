# Security policy

## Supported scope

This repository implements one relaxed, single-user Windows-local bridge for private Feishu/Lark chats and Codex tasks. It runs under the current Windows user and is not a hosted or multi-user service.

## Local trust model

- The Windows user, the configured Feishu/Lark tenant and app, mapped private chats, the Codex installation, and project files are trusted together.
- Any human user who can post in a mapped private topic can submit text, images, files, controls, and approval actions that are enabled in the local configuration. Sender Open ID and display name are not authorization gates.
- Bot, project, task, group, and user names are mutable metadata. Stable tenant, app, project, task, chat, message, and reply IDs determine routing.
- The bridge and Codex writer run under the same Windows user. `dangerFullAccess`, network access, and `approval_policy = "never"` are supported without a second account.
- A live Feishu/Lark preflight failure is diagnostic and does not stop the service unless the configured tenant or app ID conflicts with the contract.

## Reliability retained in relaxed mode

- Provider credentials remain in Windows Credential Manager; OAuth tokens remain under the official `lark-cli`; neither belongs in TOML, logs, evidence, plugins, or Git.
- SQLite queues, provider UUIDs, exact message ancestry, rollout item IDs, task/chat bindings, fencing tokens, a single-instance mutex, attachment hashes, and bounded file sizes prevent accidental cross-task routing, duplicate sends, and corrupted retries.
- Project-root moves and all display-name changes refresh metadata in place. A chat-ID or task-ID change creates a different route and is never inferred from a matching name.
- Attachments are stored under the selected project's inbox and are never auto-executed or auto-extracted. Local paths are not exposed in Feishu/Lark message bodies.
- Approval tokens stay short-lived and single-use, but any human operator in the mapped private chat may use them.

## Reporting and evidence

Do not include real message bodies, prompts, tokens, secrets, tenant exports, chat IDs, Open IDs, opaque actions, or full approval payloads in public issues or evidence. Report hashes, state transitions, timestamps, endpoint names, and reproducible fixtures.
