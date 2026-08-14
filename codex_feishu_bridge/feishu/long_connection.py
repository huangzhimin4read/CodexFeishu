"""Official SDK long-connection runner with registered P2P handlers."""

from __future__ import annotations

from typing import Any

import lark_oapi as lark
from lark_oapi import ws
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

from .credentials import WindowsCredentialManager
from .events import EventHandlers


class FeishuLongConnection:
    def __init__(
        self,
        *,
        app_id: str,
        credential_target: str,
        handlers: EventHandlers,
    ) -> None:
        secret = WindowsCredentialManager().read(credential_target)

        def message_handler(event: Any) -> None:
            handlers.message_receive(event)

        def card_handler(event: Any) -> P2CardActionTriggerResponse:
            return P2CardActionTriggerResponse(handlers.card_action(event))

        dispatcher = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(message_handler)
            .register_p2_card_action_trigger(card_handler)
            .build()
        )
        self.client = ws.Client(
            app_id,
            secret.app_secret,
            log_level=lark.LogLevel.WARNING,
            event_handler=dispatcher,
            auto_reconnect=True,
        )

    def start(self) -> None:
        self.client.start()

    def connection_state(self) -> str:
        """Return a body-free best-effort SDK transport state.

        The pinned SDK keeps ``_conn`` non-null only while the websocket is
        established and clears it before reconnecting. Reading the reference is
        side-effect free; the service separately verifies that the owning
        thread is alive.
        """

        return "connected" if getattr(self.client, "_conn", None) is not None else "connecting"
