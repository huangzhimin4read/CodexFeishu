# Security policy

## Supported scope

This repository implements an owner-operated local bridge for one pre-bound Feishu P2P chat or activity-triggered private project topic groups and explicitly allowlisted Codex tasks. It is not a multi-user service and does not authorize ordinary/public groups, public webhooks, arbitrary tenant users, or arbitrary local projects.

## Trust boundaries

- Provider credentials are read from Windows Credential Manager and never belong in TOML, logs, evidence, or chat output.
- Approval/audit keys are protected with CurrentUser DPAPI. Approval actions use short-lived, single-use opaque tokens bound to tenant, app, operator, chat, card message, task epoch, identity epoch, and kill generation.
- `dangerFullAccess` is refused unless App Server traffic uses the separately-principalled worker transport. The worker receives no provider credential, broker database, or approval key. Closing its Job Object kills the App Server subprocess.
- Outbound-only Desktop tasks remain mirror-only. When the explicit topic-group remote extension is enabled, a Desktop task becomes remotely writable only through an active per-task grant bound to its exact project root, project chat, anchor/root, task binding epoch, owner identity epoch, service fencing token and capability hash. Dispatch additionally requires an idle resumed thread, `canAcceptDirectInput=true`, and a complete managed-policy-valid execution profile.
- Unknown provider sends, App Server write windows, approval responses, restore lineage, reply ancestry, and selection order fail closed; they are never retried as a new logical operation without reconciliation.
- Local image references are a narrow data-egress capability: only a visible assistant Markdown image whose canonical file stays inside the bound task project may be read. Remote URLs are not fetched; Windows ambiguous paths, reparse points, unsupported/mismatched formats, empty files, files over 10 MiB, and identity changes are rejected. Immutable bytes are captured before provider work, and local paths never enter Feishu message bodies or evidence.
- Feishu-originated images/files are downloaded only from the exact message-resource endpoint after owner/chat/root routing has been fixed. Immutable bytes are bounded and hashed, then materialized under the target project's `.codex-feishu-inbox`; image inputs use a typed local-image item, while other files are referenced as data and are never auto-executed.
- Any remote capability requires a different, non-administrator Windows worker principal. The worker may access the selected Codex state and project roots required to resume tasks. The bridge `.runtime` tree is protected for broker/SYSTEM/Administrators only, and the Feishu App Secret remains in the broker user's Credential Manager. Same-principal fallback is a configuration error.
- Live broker and worker entrypoints run from a broker-owned, worker-read-only package staged below `ProgramData`; the live config and frozen tenant contract are broker-only copies below `.runtime`. A remotely writable project checkout is never an executable/configuration authority for the next broker start.
- The worker-accessible Codex home necessarily contains the Codex session/auth state needed to resume the owner's tasks. Those Codex credentials are inside the worker trust boundary: `dangerFullAccess` plus network access can expose them. Use a dedicated Codex account/home if that owner-operated risk is unacceptable. Feishu credentials, approval keys, audit material, and runtime databases remain outside that boundary.

## Reporting and evidence

Do not include message bodies, prompts, tokens, secrets, opaque actions, or full approval payloads in issues or evidence. Report hashes, state transitions, timestamps, endpoint names, and reproducible local fixtures. A local PASS does not authorize tenant publishing, Windows account installation, or production release.
