"""飞书官方 SDK 适配：消息、静态卡片和 CardKit 原生流式卡片。"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

from .cards import permission_card, reply_card, stream_metrics, streaming_card
from ..app.service import IncomingMessage, ReplySnapshot

log = logging.getLogger(__name__)
HandleMessage = Callable[[IncomingMessage], Awaitable[None]]


def compact_content(snapshot: ReplySnapshot) -> str:
    """返回 CardKit 普通文本元素需要的完整内容，并在终态附加指标。"""
    card = streaming_card(snapshot)
    content = card["body"]["elements"][0]["content"]
    if snapshot.final:
        content = f"{content}\n\n---\n\n<font color='grey'>{stream_metrics(snapshot.metrics, snapshot.session_id)}</font>"
    return content


@dataclass
class StreamingReplyHandle:
    """一条 CardKit 流式卡片消息的标识和更新序号。"""

    message_id: str = ""
    card_id: str = ""
    element_id: str = "stream_md"
    sequence: int = 0
    request_uuid: str = ""
    last_state: str = ""


class FeishuBot:
    """官方 OAPI 适配层，业务规则保留在 BotService。"""

    def __init__(self, app_id: str, app_secret: str, on_message: HandleMessage, on_card_action: Callable[[dict[str, str], str, str], bool]) -> None:
        self.app_id, self.app_secret = app_id, app_secret
        self.on_message, self.on_card_action = on_message, on_card_action
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        self.loop: asyncio.AbstractEventLoop | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """启动飞书 WebSocket 长连接。"""
        self.loop = loop
        dispatcher = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(self._on_message).register_p2_card_action_trigger(self._on_card_action).build()
        lark.ws.Client(self.app_id, self.app_secret, event_handler=dispatcher).start()

    def _on_message(self, event: Any) -> None:
        """将飞书文本事件切换到主 asyncio 循环。"""
        message = event.event.message
        sender = event.event.sender.sender_id.open_id
        if message.message_type != "text" or not sender:
            return
        text = json.loads(message.content).get("text", "").strip()
        if text and self.loop:
            self.loop.call_soon_threadsafe(asyncio.create_task, self.on_message(IncomingMessage(message.chat_id, sender, text, message.message_id)))

    def _on_card_action(self, event: Any) -> Any:
        """校验并将工具授权卡片点击切换到主 asyncio 循环。"""
        data = event.event
        action = getattr(data, "action", None)
        value = getattr(action, "value", {}) or {}
        if not isinstance(value, dict):
            value = dict(value) if hasattr(value, "items") else {}
        operator = getattr(data, "operator", None)
        context_data = getattr(data, "context", None)
        actor = getattr(operator, "open_id", "")
        context = getattr(context_data, "open_chat_id", "")
        log.info("收到飞书授权点击：approval_id=%s，decision=%s，用户=%s，群聊=%s", value.get("approval_id", ""), value.get("decision", ""), actor or "未知", context or "未知")
        if not self.loop:
            log.error("授权点击处理失败：机器人主事件循环尚未就绪")
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "机器人尚未就绪"}})
        self.loop.call_soon_threadsafe(self._dispatch_card_action, value, context, actor)
        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "已提交授权"}})

    def _dispatch_card_action(self, value: dict[str, str], chat_id: str, user_open_id: str) -> None:
        """在主事件循环中执行授权解析并记录结果。"""
        resolved = self.on_card_action(value, chat_id, user_open_id)
        log.info("授权解析结果：approval_id=%s，结果=%s", value.get("approval_id", ""), "成功" if resolved else "失败，授权已过期或身份不匹配")

    async def send_text(self, chat_id: str, text: str) -> None:
        """发送普通文本消息。"""
        body = lark.im.v1.CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("text").content(json.dumps({"text": text})).build()
        request = lark.im.v1.CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        response = await asyncio.to_thread(self.client.im.v1.message.create, request)
        if not response.success():
            raise RuntimeError(f"feishu send failed: {response.code} {response.msg}")

    async def send_card(self, chat_id: str, card: dict[str, Any]) -> str:
        """发送普通静态 interactive 卡片，用于权限授权。"""
        body = lark.im.v1.CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("interactive").content(json.dumps(card)).build()
        request = lark.im.v1.CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        response = await asyncio.to_thread(self.client.im.v1.message.create, request)
        if not response.success():
            raise RuntimeError(f"feishu send failed: {response.code} {response.msg}")
        return response.data.message_id

    async def delete_message(self, message_id: str) -> None:
        """删除已经完成授权的工具授权卡片。"""
        request = lark.im.v1.DeleteMessageRequest.builder().message_id(message_id).build()
        response = await asyncio.to_thread(self.client.im.v1.message.delete, request)
        if not response.success():
            log.warning("授权卡片删除失败：消息=%s，错误码=%s，错误信息=%s", message_id, response.code, response.msg)

    async def create_streaming_reply(self, chat_id: str, snapshot: ReplySnapshot) -> StreamingReplyHandle:
        """直接发送 interactive 流式卡片，便于对比旧模式首屏延迟。"""
        log.info("开始创建 interactive 流式卡片：状态=%s，正文长度=%d", snapshot.state, len(snapshot.text))
        body = lark.im.v1.CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("interactive").content(json.dumps(streaming_card(snapshot), ensure_ascii=False)).build()
        request = lark.im.v1.CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        response = await asyncio.to_thread(self.client.im.v1.message.create, request)
        if not response.success():
            raise RuntimeError(f"feishu stream create failed: {response.code} {response.msg}")
        log.info("interactive 流式卡片发送完成")
        return StreamingReplyHandle(response.data.message_id, request_uuid=str(uuid.uuid4()))

    async def update_streaming_reply(self, handle: StreamingReplyHandle, snapshot: ReplySnapshot) -> None:
        """使用旧版 interactive 消息 Patch 更新流式卡片。"""
        if not handle.message_id:
            raise RuntimeError("interactive update unavailable: streaming reply has no message_id")
        body = lark.im.v1.PatchMessageRequestBody.builder().content(json.dumps(streaming_card(snapshot), ensure_ascii=False)).build()
        request = lark.im.v1.PatchMessageRequest.builder().message_id(handle.message_id).request_body(body).build()
        response = await asyncio.to_thread(self.client.im.v1.message.patch, request)
        if not response.success():
            log.error("飞书 interactive 卡片更新失败：消息=%s，状态=%s，错误码=%s，错误信息=%s", handle.message_id, snapshot.state, response.code, response.msg)
            raise RuntimeError(f"feishu stream patch failed: {response.code} {response.msg}")

    def build_permission_card(self, approval_id: str, token: str, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """构建工具授权卡。"""
        return permission_card(approval_id, token, tool_name, tool_input)

    def build_reply_card(self, snapshot: ReplySnapshot) -> dict[str, Any]:
        """保留旧静态回复卡构建入口，兼容外部调用。"""
        return reply_card(snapshot.text, snapshot.state, snapshot.detail, snapshot.metrics, snapshot.final, snapshot.steps)
