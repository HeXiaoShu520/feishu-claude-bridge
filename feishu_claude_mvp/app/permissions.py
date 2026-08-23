from __future__ import annotations

import asyncio
import secrets
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class PendingPermission:
    chat_id: str
    user_open_id: str
    token: str
    future: asyncio.Future[str]


class PermissionBroker:
    """Keeps short-lived approvals in memory; callback identity is always checked."""

    def __init__(self, timeout: int = 300) -> None:
        self.timeout = timeout
        self.pending: dict[str, PendingPermission] = {}

    async def request(self, chat_id: str, user_open_id: str, send_card, tool_name: str, tool_input: dict[str, Any]) -> str:
        approval_id, token = str(uuid.uuid4()), secrets.token_urlsafe(24)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.pending[approval_id] = PendingPermission(chat_id, user_open_id, token, future)
        message_id = await send_card(approval_id, token, tool_name, tool_input)
        try:
            decision = await asyncio.wait_for(future, self.timeout)
            return decision
        except asyncio.TimeoutError:
            return "deny"
        finally:
            self.pending.pop(approval_id, None)

    def resolve(self, approval_id: str, token: str, decision: str, chat_id: str, user_open_id: str) -> bool:
        """校验授权回调身份并唤醒等待中的 Claude 工具请求。"""
        pending = self.pending.get(approval_id)
        if not pending:
            return False
        if pending.token != token or pending.chat_id != chat_id or pending.user_open_id != user_open_id:
            return False
        if decision not in {"allow_once", "allow_session", "deny"} or pending.future.done():
            return False
        pending.future.set_result(decision)
        return True

    def cancel_chat(self, chat_id: str, user_open_id: str) -> None:
        for pending in list(self.pending.values()):
            if pending.chat_id == chat_id and pending.user_open_id == user_open_id and not pending.future.done():
                pending.future.set_result("deny")
