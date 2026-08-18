"""Bridge App Server approval requests to context-bound Feishu cards."""

from __future__ import annotations

import json
import uuid
from hashlib import sha256
from typing import Any

from ..runtime_config import FeishuBinding
from ..runtime_storage import RuntimeStorage, utc_now
from ..security.approvals import (
    ApprovalBroker,
    ApprovalContext,
    ApprovalError,
)
from ..security.jcs import canonicalize
from ..feishu.formatter import redact_text
from ..feishu.outbound import stable_uuid
from .connection import AppServerConnection


class ApprovalGatewayError(RuntimeError):
    pass


_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
}


class ApprovalGateway:
    def __init__(
        self,
        storage: RuntimeStorage,
        connection: AppServerConnection,
        broker: ApprovalBroker,
        binding: FeishuBinding,
        *,
        server_epoch: str,
        connection_epoch: str,
        session_id: str,
        auto_approve: bool = False,
    ) -> None:
        self.storage = storage
        self.connection = connection
        self.broker = broker
        self.binding = binding
        self.server_epoch = server_epoch
        self.connection_epoch = connection_epoch
        self.session_id = session_id
        self.auto_approve = auto_approve

    def publish_next_request(self) -> bool:
        try:
            request = self.connection.server_requests.get_nowait()
        except Exception:
            return False
        method = request.get("method")
        params = request.get("params")
        request_id = request.get("id")
        if not isinstance(request_id, (int, str)):
            raise ApprovalGatewayError("deferred server request lacks an identity")
        if method not in _APPROVAL_METHODS:
            # Stable App Server methods such as item/tool/call may be valid on
            # the wire while remaining deliberately unavailable to this
            # local bridge. Reject that one unsupported request explicitly; killing
            # the shared Broker would strand unrelated Feishu traffic and make
            # a supervisor restart loop without granting any capability.
            self.connection.respond_error(
                request_id,
                code=-32601,
                message="Server request is not available in the remote bridge",
            )
            return True
        if not isinstance(params, dict):
            self.connection.respond_error(
                request_id,
                code=-32602,
                message="Server request parameters are invalid",
            )
            return True
        thread_id = str(params.get("threadId") or params.get("conversationId") or "")
        turn_id = params.get("turnId")
        if not thread_id:
            raise ApprovalGatewayError("approval request lacks a thread identity")
        task = self.storage.connection.execute(
            "SELECT anchor_message_id,current_binding_epoch,identity_binding_epoch,chat_id,opted_in "
            "FROM task_bindings WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        if (
            task is None
            or not task["opted_in"]
            or not self._active_project_chat(str(task["chat_id"]))
            or not self._approval_is_authorized(thread_id, task)
        ):
            raise ApprovalGatewayError("approval thread is not bound to the owner chat")
        task_chat_id = str(task["chat_id"])
        service = self.storage.connection.execute(
            "SELECT kill_generation,fencing_token FROM service_state WHERE singleton=1"
        ).fetchone()
        request_id = str(request_id)
        upstream_approval_id = str(params.get("approvalId") or "")
        approval_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "approval:"
                + self.connection_epoch
                + ":"
                + request_id
                + ":"
                + upstream_approval_id,
            )
        )
        decision_map = _decision_map(method, params)
        # The eventual callback context contains the created card message id. A
        # stable logical placeholder is bound now and replaced after delivery.
        card_logical_id = "approval-card:" + approval_id
        context = ApprovalContext(
            approval_id=approval_id,
            server_request_id=request_id,
            server_method=method,
            thread_id=thread_id,
            turn_id=str(turn_id) if turn_id else None,
            tenant_key=self.binding.tenant_key,
            app_id=self.binding.app_id,
            chat_id=task_chat_id,
            card_message_id=card_logical_id,
            operator_open_id=self.binding.owner_open_id,
            binding_epoch=int(task["current_binding_epoch"]),
            identity_binding_epoch=int(task["identity_binding_epoch"]),
            kill_generation=int(service["kill_generation"]),
            server_epoch=self.server_epoch,
            connection_epoch=self.connection_epoch,
            session_id=self.session_id,
            service_fencing_token=int(service["fencing_token"]),
        )
        try:
            issued = self.broker.issue(
                context=context,
                request=params,
                decision_map=decision_map,
            )
        except ApprovalError as exc:
            self.connection.respond_error(
                _request_id_value(request_id),
                code=-32600,
                message="Conflicting duplicate approval request",
            )
            return True
        if not issued.created:
            return True
        assert issued.opaque_token is not None
        automatic_decision = (
            _automatic_decision(method, decision_map) if self.auto_approve else None
        )
        if automatic_decision is not None:
            approval_id, response = self.broker.commit_card_action(
                opaque_token=issued.opaque_token,
                decision=automatic_decision,
                tenant_key=context.tenant_key,
                app_id=context.app_id,
                chat_id=context.chat_id,
                card_message_id=context.card_message_id,
                operator_open_id=context.operator_open_id,
                binding_epoch=context.binding_epoch,
                identity_binding_epoch=context.identity_binding_epoch,
                kill_generation=context.kill_generation,
                server_epoch=context.server_epoch,
                connection_epoch=context.connection_epoch,
                session_id=context.session_id,
                service_fencing_token=context.service_fencing_token,
            )
            self.broker.commit_exact_response(approval_id, response)
            try:
                self.connection.respond(_request_id_value(request_id), response)
            except Exception:
                self.broker.record_response_result(approval_id, accepted=None)
                return True
            self.broker.record_response_result(approval_id, accepted=None)
            return True
        card = _approval_card(method, issued.opaque_token, tuple(decision_map), params)
        body_json = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        now = utc_now()
        self.storage.enqueue_provider_message(
            logical_message_id=card_logical_id,
            thread_id=thread_id,
            turn_id=str(turn_id) if turn_id else None,
            operation="approval",
            endpoint_name="reply_message",
            target_message_id=task["anchor_message_id"],
            reply_in_thread=self.binding.reply_in_thread,
            stable_uuid=stable_uuid(
                card_logical_id,
                chat_id=task_chat_id,
                conversation_mode=self.binding.conversation_mode.value,
            ),
            marker="approval:" + approval_id[:24],
            body_json=body_json,
            body_hash=sha256(body_json.encode("utf-8")).hexdigest(),
            priority=200,
        )
        return True

    def bind_delivered_card(self, approval_id: str, provider_message_id: str) -> None:
        updated = self.storage.connection.execute(
            "UPDATE approval_actions SET card_message_id=? WHERE approval_id=? "
            "AND card_message_id=?",
            (provider_message_id, approval_id, "approval-card:" + approval_id),
        )
        if updated.rowcount != 1:
            raise ApprovalGatewayError("approval card binding lost compare-and-swap")

    def handle_card_action(self, callback: dict[str, Any]) -> dict[str, Any]:
        header = callback.get("header")
        event = callback.get("event") or callback
        if not isinstance(header, dict) or not isinstance(event, dict):
            raise ApprovalGatewayError("invalid card callback envelope")
        operator = event.get("operator")
        context = event.get("context")
        action = event.get("action")
        if not isinstance(operator, dict) or not isinstance(context, dict) or not isinstance(action, dict):
            raise ApprovalGatewayError("card callback lacks operator/context/action")
        value = action.get("value")
        if not isinstance(value, dict):
            raise ApprovalGatewayError("card action value must be an object")
        token = value.get("token")
        decision = value.get("decision")
        if not isinstance(token, str) or not isinstance(decision, str):
            raise ApprovalGatewayError("card action lacks opaque token/decision")
        if header.get("tenant_key") != self.binding.tenant_key or header.get("app_id") != self.binding.app_id:
            raise ApprovalGatewayError("card callback tenant/app mismatch")
        open_id = operator.get("open_id") or (operator.get("operator_id") or {}).get("open_id")
        chat_id = context.get("open_chat_id")
        message_id = context.get("open_message_id")
        approval_binding = self.storage.connection.execute(
            "SELECT a.thread_id,t.current_binding_epoch,t.identity_binding_epoch,t.chat_id "
            "FROM approval_actions a JOIN task_bindings t "
            "ON t.thread_id=a.thread_id WHERE a.chat_id=? AND a.card_message_id=? "
            "AND t.chat_id=a.chat_id AND t.opted_in=1",
            (chat_id, message_id),
        ).fetchone()
        identity = self.storage.connection.execute(
            "SELECT binding_epoch,state FROM identity_bindings WHERE binding_key='owner'"
        ).fetchone()
        service = self.storage.connection.execute(
            "SELECT kill_generation,fencing_token FROM service_state WHERE singleton=1"
        ).fetchone()
        if (
            approval_binding is None
            or identity is None
            or identity["state"] != "active"
            or service is None
            or not isinstance(chat_id, str)
            or not isinstance(open_id, str)
            or not open_id
            or not self._active_project_chat(chat_id)
            or not self._approval_is_authorized(
                str(approval_binding["thread_id"]), approval_binding
            )
        ):
            raise ApprovalGatewayError("approval control bindings are inactive")
        approval_id, response = self.broker.commit_card_action(
            opaque_token=token,
            decision=decision,
            tenant_key=str(header["tenant_key"]),
            app_id=str(header["app_id"]),
            chat_id=str(chat_id),
            card_message_id=str(message_id),
            operator_open_id=str(open_id),
            binding_epoch=int(approval_binding["current_binding_epoch"]),
            identity_binding_epoch=int(identity["binding_epoch"]),
            kill_generation=int(service["kill_generation"]),
            server_epoch=self.server_epoch,
            connection_epoch=self.connection_epoch,
            session_id=self.session_id,
            service_fencing_token=int(service["fencing_token"]),
        )
        response = _resolve_dynamic_response(response, action)
        row = self.storage.connection.execute(
            "SELECT server_request_id FROM approval_actions WHERE approval_id=?", (approval_id,)
        ).fetchone()
        self.broker.commit_exact_response(approval_id, response)
        try:
            self.connection.respond(_request_id_value(row["server_request_id"]), response)
        except Exception:
            self.broker.record_response_result(approval_id, accepted=None)
            return {"toast": {"type": "warning", "content": "决定已记录，Codex 响应结果未知"}}
        # App Server response writes have no response-ack. Resolution is only
        # promoted by serverRequest/resolved; until then outcome stays unknown.
        self.broker.record_response_result(approval_id, accepted=None)
        return {"toast": {"type": "success", "content": "决定已提交，等待 Codex 确认"}}

    def _active_project_chat(self, chat_id: str) -> bool:
        if self.binding.conversation_mode.value == "p2p":
            return chat_id == self.binding.target_chat_id
        row = self.storage.connection.execute(
            "SELECT 1 FROM project_groups WHERE chat_id=? AND state='active'", (chat_id,)
        ).fetchone()
        return row is not None

    def _approval_is_authorized(self, thread_id: str, task: Any) -> bool:
        service = self.storage.connection.execute(
            "SELECT fencing_token,process_state FROM service_state WHERE singleton=1"
        ).fetchone()
        if service is None or service["process_state"] != "running":
            return False
        ownership = self.storage.connection.execute(
            "SELECT ownership_state FROM thread_bindings WHERE thread_id=?", (thread_id,)
        ).fetchone()
        if ownership is not None and ownership["ownership_state"] == "bridge_owned":
            return True
        grant = self.storage.connection.execute(
            "SELECT * FROM remote_task_grants WHERE thread_id=? AND state='active'", (thread_id,)
        ).fetchone()
        if grant is None:
            return False
        try:
            capabilities = json.loads(grant["capabilities_json"])
        except (TypeError, json.JSONDecodeError):
            return False
        return (
            capabilities.get("approvals") is True
            and sha256(canonicalize(capabilities)).hexdigest() == grant["capabilities_hash"]
            and grant["chat_id"] == task["chat_id"]
            and int(grant["task_binding_epoch"]) == int(task["current_binding_epoch"])
            and int(grant["identity_binding_epoch"]) == int(task["identity_binding_epoch"])
            and int(grant["service_fencing_token"]) == int(service["fencing_token"])
        )

    def observe_resolution_notification(self, notification: dict[str, Any]) -> bool:
        if notification.get("method") != "serverRequest/resolved":
            return False
        params = notification.get("params")
        if not isinstance(params, dict):
            return False
        request_id = str(params.get("requestId"))
        row = self.storage.connection.execute(
            "SELECT a.approval_id,a.consumed_decision,r.state FROM approval_actions a "
            "JOIN approval_requests r ON r.approval_id=a.approval_id "
            "WHERE a.server_epoch=? AND a.connection_epoch=? AND a.server_request_id=?",
            (self.server_epoch, self.connection_epoch, request_id),
        ).fetchone()
        if row is None or row["state"] not in {"response_sending", "outcome_unknown"}:
            return False
        self.storage.connection.execute(
            "UPDATE approval_requests SET state='resolved',updated_at=? WHERE approval_id=? "
            "AND state IN ('response_sending','outcome_unknown')",
            (utc_now(), row["approval_id"]),
        )
        if row["consumed_decision"] in {"acceptForSession", "approved_for_session"}:
            service = self.storage.connection.execute(
                "SELECT kill_generation FROM service_state WHERE singleton=1"
            ).fetchone()
            self.storage.connection.execute(
                "INSERT INTO active_grants(grant_id,server_epoch,connection_epoch,session_id,kill_generation,expires_at) "
                "VALUES(?,?,?,?,?,datetime('now','+8 hours'))",
                (
                    "grant:" + row["approval_id"],
                    self.server_epoch,
                    self.connection_epoch,
                    self.session_id,
                    service["kill_generation"],
                ),
            )
        return True


def _request_id_value(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        return value


def _automatic_decision(
    method: str, decision_map: dict[str, dict[str, Any]]
) -> str | None:
    """Choose the strongest positive approval without answering user questions."""

    if method in {"item/tool/requestUserInput", "mcpServer/elicitation/request"}:
        return None
    for decision in (
        "acceptForSession",
        "approved_for_session",
        "accept",
        "approved",
    ):
        if decision in decision_map:
            return decision
    return None


def _decision_map(method: str, params: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if method == "item/commandExecution/requestApproval":
        offered = params.get("availableDecisions")
        allowed = ["accept", "acceptForSession", "decline", "cancel"]
        if isinstance(offered, list):
            allowed = [value for value in allowed if value in offered]
        return {value: {"decision": value} for value in allowed}
    if method == "item/fileChange/requestApproval":
        return {value: {"decision": value} for value in ("accept", "acceptForSession", "decline", "cancel")}
    if method in {"execCommandApproval", "applyPatchApproval"}:
        return {
            "approved": {"decision": "approved"},
            "approved_for_session": {"decision": "approved_for_session"},
            "deny": {"decision": {"denied": {"rejection": "Denied by owner"}}},
            "abort": {"decision": "abort"},
        }
    if method == "item/permissions/requestApproval":
        requested = params.get("permissions")
        return {
            "accept": {"permissions": requested, "scope": "turn"},
            "decline": {"permissions": {}, "scope": "turn"},
        }
    if method == "item/tool/requestUserInput":
        questions = params.get("questions")
        if not isinstance(questions, list) or not questions:
            raise ApprovalGatewayError("requestUserInput has no questions")
        fields: dict[str, Any] = {}
        for question in questions:
            if not isinstance(question, dict) or not isinstance(question.get("id"), str):
                raise ApprovalGatewayError("requestUserInput question identity is invalid")
            options = question.get("options")
            fields[question["id"]] = {
                "options": [item.get("label") for item in options if isinstance(item, dict)]
                if isinstance(options, list)
                else None,
                "secret": bool(question.get("isSecret")),
            }
        return {"submit": {"__dynamic__": "request_input", "fields": fields}}
    if method == "mcpServer/elicitation/request":
        mode = params.get("mode")
        if mode == "form":
            return {
                "submit": {
                    "__dynamic__": "mcp_form",
                    "schema": params.get("requestedSchema"),
                },
                "decline": {"action": "decline", "content": None},
                "cancel": {"action": "cancel", "content": None},
            }
        return {
            "decline": {"action": "decline", "content": None},
            "cancel": {"action": "cancel", "content": None},
        }
    raise ApprovalGatewayError("method has no approval decision map")


def _approval_card(
    method: str,
    token: str,
    decisions: tuple[str, ...],
    params: dict[str, Any],
) -> dict[str, Any]:
    if method == "item/tool/requestUserInput":
        elements: list[dict[str, Any]] = []
        for question in params.get("questions", []):
            elements.append(
                {
                    "tag": "input",
                    "name": question["id"],
                    "required": True,
                    "placeholder": {"tag": "plain_text", "content": question["question"]},
                    "label": {"tag": "plain_text", "content": question["header"]},
                }
            )
        elements.append(
            {
                "tag": "button",
                "name": "submit",
                "action_type": "form_submit",
                "type": "primary",
                "text": {"tag": "plain_text", "content": "提交"},
                "value": {"token": token, "decision": "submit"},
            }
        )
        return {
            "header": {"title": {"tag": "plain_text", "content": "Codex 请求输入"}},
            "elements": [{"tag": "form", "name": "codex_input", "elements": elements}],
        }
    if method == "mcpServer/elicitation/request" and params.get("mode") == "form":
        schema = params.get("requestedSchema")
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = set(schema.get("required", [])) if isinstance(schema, dict) else set()
        elements = [
            {
                "tag": "input",
                "name": name,
                "required": name in required,
                "placeholder": {"tag": "plain_text", "content": str(value.get("description") or name)},
                "label": {"tag": "plain_text", "content": name},
            }
            for name, value in properties.items()
            if isinstance(value, dict) and value.get("type") in {"string", "number", "integer", "boolean"}
        ]
        elements.append(
            {
                "tag": "button",
                "name": "submit",
                "action_type": "form_submit",
                "type": "primary",
                "text": {"tag": "plain_text", "content": "提交"},
                "value": {"token": token, "decision": "submit"},
            }
        )
        return {
            "header": {"title": {"tag": "plain_text", "content": "MCP 请求输入"}},
            "elements": [
                {"tag": "form", "name": "mcp_input", "elements": elements},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": label},
                            "type": "default",
                            "value": {"token": token, "decision": decision},
                        }
                        for decision, label in (("decline", "拒绝"), ("cancel", "取消"))
                        if decision in decisions
                    ],
                },
            ],
        }
    buttons = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": decision},
            "type": "primary" if decision in {"accept", "approved"} else "default",
            "value": {"token": token, "decision": decision},
        }
        for decision in decisions
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "Codex 需要你的决定"}},
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "plain_text", "content": _approval_summary(method, params)},
            },
            {"tag": "action", "actions": buttons},
        ],
    }


def _approval_summary(method: str, params: dict[str, Any]) -> str:
    """Produce a bounded, redacted decision summary without patch bodies."""
    lines = [method]
    command = params.get("command")
    if isinstance(command, list):
        command = " ".join(str(value) for value in command)
    if isinstance(command, str) and command.strip():
        lines.append("命令：" + command.strip())
    cwd = params.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        lines.append("目录：" + cwd.strip())
    reason = params.get("reason")
    if isinstance(reason, str) and reason.strip():
        lines.append("原因：" + reason.strip())
    grant_root = params.get("grantRoot")
    if isinstance(grant_root, str) and grant_root.strip():
        lines.append("会话写入根：" + grant_root.strip())
    changes = params.get("fileChanges")
    if isinstance(changes, dict):
        descriptions: list[str] = []
        for path, detail in sorted(changes.items(), key=lambda item: str(item[0]))[:20]:
            kind = detail.get("type") if isinstance(detail, dict) else "unknown"
            descriptions.append(f"{path} ({kind})")
        lines.append("文件：" + "；".join(descriptions))
        if len(changes) > 20:
            lines.append(f"另有 {len(changes) - 20} 个文件")
    for label, field in (
        ("附加权限", "additionalPermissions"),
        ("请求权限", "permissions"),
        ("网络上下文", "networkApprovalContext"),
    ):
        value = params.get(field)
        if value is not None:
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            lines.append(f"{label}：{rendered}")
    return redact_text("\n".join(lines))[:2000]


def _resolve_dynamic_response(response: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    kind = response.get("__dynamic__")
    if kind is None:
        return response
    form = action.get("form_value") or action.get("formValue")
    if not isinstance(form, dict):
        raise ApprovalGatewayError("form submission lacks form values")
    if kind == "request_input":
        fields = response.get("fields")
        if not isinstance(fields, dict) or set(form) != set(fields):
            raise ApprovalGatewayError("request input fields do not match the issued form")
        answers: dict[str, dict[str, list[str]]] = {}
        for field, rules in fields.items():
            value = form[field]
            values = [str(item) for item in value] if isinstance(value, list) else [str(value)]
            options = rules.get("options") if isinstance(rules, dict) else None
            if options and any(item not in options for item in values):
                raise ApprovalGatewayError("request input answer is outside the offered options")
            answers[field] = {"answers": values}
        return {"answers": answers}
    if kind == "mcp_form":
        schema = response.get("schema")
        if not isinstance(schema, dict):
            raise ApprovalGatewayError("MCP form schema is missing")
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        if not isinstance(properties, dict) or not required.issubset(form) or set(form) - set(properties):
            raise ApprovalGatewayError("MCP form values violate the issued field set")
        content: dict[str, Any] = {}
        for name, value in form.items():
            field_type = properties[name].get("type") if isinstance(properties[name], dict) else None
            if field_type == "integer":
                content[name] = int(value)
            elif field_type == "number":
                content[name] = float(value)
            elif field_type == "boolean":
                content[name] = str(value).lower() in {"1", "true", "yes", "on"}
            else:
                content[name] = str(value)
        return {"action": "accept", "content": content}
    raise ApprovalGatewayError("unknown dynamic approval response kind")
