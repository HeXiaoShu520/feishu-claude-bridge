"""飞书官方 SDK 适配：消息、静态卡片和 CardKit 原生流式卡片。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

from .cards import cardkit_streaming_card, permission_card, permission_result_card, reply_card, stream_metrics, streaming_card
from ..app.service import IncomingMessage, ReplySnapshot

log = logging.getLogger(__name__)
HandleMessage = Callable[[IncomingMessage], Awaitable[None]]


def compact_content(snapshot: ReplySnapshot) -> str:
    """返回 CardKit 普通文本元素需要的完整内容，并在终态附加指标。"""
    card = streaming_card(snapshot)
    content = card["elements"][0]["content"]
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
    chat_id: str = ""
    user_open_id: str = ""


class FeishuBot:
    """官方 OAPI 适配层，业务规则保留在 BotService。"""

    def __init__(self, app_id: str, app_secret: str, on_message: HandleMessage, on_card_action: Callable[[dict[str, str], str, str], bool]) -> None:
        self.app_id, self.app_secret = app_id, app_secret
        self.on_message, self.on_card_action = on_message, on_card_action
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ws_client: Any = None
        self.ws_thread: threading.Thread | None = None
        self.stop_requested = threading.Event()
        self._stop_lock = threading.Lock()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """启动飞书 WebSocket 长连接。"""
        self.ws_thread = threading.current_thread()
        self.loop = loop
        dispatcher = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(self._on_message).register_p2_card_action_trigger(self._on_card_action).register_p2_im_message_reaction_created_v1(self._ignore_reaction_event).register_p2_im_message_reaction_deleted_v1(self._ignore_reaction_event).build()
        log.info("正在启动飞书 WebSocket：app_id=%s", self.app_id)
        self.ws_client = lark.ws.Client(self.app_id, self.app_secret, event_handler=dispatcher, auto_reconnect=False)
        if self.stop_requested.is_set():
            self.ws_client = None
            return
        try:
            self.ws_client.start()
        except (KeyboardInterrupt, SystemExit):
            self.stop_requested.set()
            log.info("收到退出信号，正在关闭飞书 WebSocket")
            raise
        finally:
            log.info("飞书 WebSocket 已停止")
            self.ws_client = None

    def stop(self) -> None:
        """请求停止 WebSocket，并等待启动线程结束。"""
        with self._stop_lock:
            if self.stop_requested.is_set():
                return
            self.stop_requested.set()
            ws_client = self.ws_client
            ws_thread = self.ws_thread
        if ws_client is not None:
            sdk_loop = getattr(lark.ws.client, "loop", None)
            connection = getattr(ws_client, "_conn", None)
            if sdk_loop is not None and connection is not None and not sdk_loop.is_closed():
                asyncio.run_coroutine_threadsafe(connection.close(), sdk_loop)
                sdk_loop.call_soon_threadsafe(sdk_loop.stop)
        if ws_thread is not None and ws_thread is not threading.current_thread():
            ws_thread.join(timeout=2)

    def _ignore_reaction_event(self, event: Any) -> None:
        """忽略表情事件，避免 SDK 因未注册处理器报错。"""
        return

    def _on_message(self, event: Any) -> None:
        """将飞书文本事件切换到主 asyncio 循环。"""
        message = event.event.message
        sender = event.event.sender.sender_id.open_id
        if message.message_type != "text" or not sender:
            return
        text = json.loads(message.content).get("text", "").strip()
        log.info("收到飞书输入：message_id=%s，chat_id=%s，user=%s，内容=%s", message.message_id, message.chat_id, sender, text)
        if text and self.loop:
            self.loop.call_soon_threadsafe(asyncio.create_task, self.on_message(IncomingMessage(message.chat_id, sender, text, message.message_id)))

    def _on_card_action(self, event: Any) -> Any:
        """解析权限和会话操作卡片，并切换到主 asyncio 循环。"""
        data = event.event
        if hasattr(data, "model_dump"):
            data_dict = data.model_dump(by_alias=True)
        elif hasattr(data, "to_dict"):
            data_dict = data.to_dict()
        else:
            data_dict = {}
        action = data_dict.get("action") or getattr(data, "action", None)
        if not action and data_dict:
            action = data_dict.get("card_action") or data_dict.get("action_data")
        if isinstance(action, dict):
            value = action.get("value", {})
        else:
            value = getattr(action, "value", {}) or {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {}
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        elif hasattr(value, "to_dict"):
            value = value.to_dict()
        elif hasattr(value, "items") and not isinstance(value, dict):
            value = dict(value)
        if not isinstance(value, dict):
            value = {}
        operator = getattr(data, "operator", None)
        context_data = getattr(data, "context", None)
        actor = getattr(operator, "open_id", "")
        context = getattr(context_data, "open_chat_id", "")
        action_name = value.get("action", "")
        is_session_action = action_name in {"details", "compact", "new"}
        if is_session_action:
            log.info("收到飞书会话操作：action=%s，用户=%s，群聊=%s", action_name, actor or "未知", context or "未知")
        else:
            log.info("收到飞书工具授权：approval_id=%s，decision=%s，用户=%s，群聊=%s", value.get("approval_id", ""), value.get("decision", ""), actor or "未知", context or "未知")
        if not self.loop:
            log.error("卡片点击处理失败：机器人主事件循环尚未就绪")
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "机器人尚未就绪"}})
        self.loop.call_soon_threadsafe(self._dispatch_card_action, value, context, actor)
        toast = "已提交操作" if is_session_action else "已提交授权"
        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": toast}})

    def _dispatch_card_action(self, value: dict[str, str], chat_id: str, user_open_id: str) -> None:
        """在主事件循环中执行卡片操作，并记录对应结果。"""
        resolved = self.on_card_action(value, chat_id, user_open_id)
        action_name = value.get("action", "")
        if action_name in {"details", "compact", "new"}:
            log.info("会话操作结果：action=%s，结果=%s", action_name, "成功" if resolved else "失败，操作未执行")
        else:
            log.info("工具授权结果：approval_id=%s，结果=%s", value.get("approval_id", ""), "成功" if resolved else "失败，授权已过期或身份不匹配")

    async def send_text(self, chat_id: str, text: str) -> None:
        """发送普通文本消息。"""
        body = lark.im.v1.CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("text").content(json.dumps({"text": text})).build()
        request = lark.im.v1.CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        log.info("发送飞书文本：chat_id=%s，内容=%s", chat_id, text)
        response = await asyncio.to_thread(self.client.im.v1.message.create, request)
        if not response.success():
            raise RuntimeError(f"feishu send failed: {response.code} {response.msg}")
        log.info("飞书文本发送完成：message_id=%s", response.data.message_id)

    async def send_card(self, chat_id: str, card: dict[str, Any]) -> str:
        """发送普通静态 interactive 卡片，用于权限授权。"""
        body = lark.im.v1.CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("interactive").content(json.dumps(card)).build()
        request = lark.im.v1.CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        response = await asyncio.to_thread(self.client.im.v1.message.create, request)
        if not response.success():
            raise RuntimeError(f"feishu send failed: {response.code} {response.msg}")
        return response.data.message_id

    async def update_permission_message(self, message_id: str, tool_name: str, tool_input: dict[str, Any], decision: str) -> None:
        """将授权卡片更新为最终授权结果。"""
        body = lark.im.v1.PatchMessageRequestBody.builder().content(json.dumps(permission_result_card(tool_name, tool_input, decision), ensure_ascii=False)).build()
        request = lark.im.v1.PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
        response = await asyncio.to_thread(self.client.im.v1.message.patch, request)
        if not response.success():
            log.warning("授权结果卡片更新失败：消息=%s，错误码=%s，错误信息=%s", message_id, response.code, response.msg)

    async def create_streaming_reply(self, message_id: str, snapshot: ReplySnapshot, user_open_id: str = "") -> StreamingReplyHandle:
        """创建 CardKit 流式卡片实体，并将其回复到原消息。"""
        card_body = lark.cardkit.v1.CreateCardRequestBody.builder().type("card_json").data(json.dumps(cardkit_streaming_card(snapshot), ensure_ascii=False)).build()
        card_request = lark.cardkit.v1.CreateCardRequest.builder().request_body(card_body).build()
        card_response = await asyncio.to_thread(self.client.cardkit.v1.card.create, card_request)
        if not card_response.success() or not card_response.data.card_id:
            raise RuntimeError(f"feishu card create failed: {card_response.code} {card_response.msg}")
        card_id = card_response.data.card_id
        reply_body = lark.im.v1.ReplyMessageRequestBody.builder().msg_type("interactive").content(json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False)).build()
        reply_request = lark.im.v1.ReplyMessageRequest.builder().message_id(message_id).request_body(reply_body).build()
        reply_response = await asyncio.to_thread(self.client.im.v1.message.reply, reply_request)
        if not reply_response.success():
            raise RuntimeError(f"feishu card reply failed: {reply_response.code} {reply_response.msg}")
        return StreamingReplyHandle(reply_response.data.message_id, card_id, request_uuid=str(uuid.uuid4()), chat_id=message_id, user_open_id=user_open_id)

    async def update_streaming_reply(self, handle: StreamingReplyHandle, snapshot: ReplySnapshot) -> None:
        """只更新 CardKit markdown element，并在终态关闭流式。"""
        if not handle.card_id:
            raise RuntimeError("cardkit update unavailable: streaming reply has no card_id")
        handle.sequence += 1
        body = lark.cardkit.v1.ContentCardElementRequestBody.builder().content(compact_content(snapshot) or "·").sequence(handle.sequence).build()
        request = lark.cardkit.v1.ContentCardElementRequest.builder().card_id(handle.card_id).element_id(handle.element_id).request_body(body).build()
        log.info("更新飞书 CardKit：card_id=%s，sequence=%s，状态=%s，内容=%s", handle.card_id, handle.sequence, snapshot.state, compact_content(snapshot))
        response = await asyncio.to_thread(self.client.cardkit.v1.card_element.content, request)
        if not response.success():
            raise RuntimeError(f"feishu card element update failed: {response.code} {response.msg}")
        if snapshot.final:
            await asyncio.sleep(min(3, len(snapshot.text) * 0.025))
            handle.sequence += 1
            settings_body = lark.cardkit.v1.SettingsCardRequestBody.builder().settings(json.dumps({"config": {"streaming_mode": False}})).sequence(handle.sequence).build()
            settings_request = lark.cardkit.v1.SettingsCardRequest.builder().card_id(handle.card_id).request_body(settings_body).build()
            settings_response = await asyncio.to_thread(self.client.cardkit.v1.card.settings, settings_request)
            if not settings_response.success():
                log.warning("关闭 CardKit 流式失败：卡片=%s，错误=%s", handle.card_id, settings_response.msg)

    def build_permission_card(self, approval_id: str, token: str, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """构建工具授权卡。"""
        return permission_card(approval_id, token, tool_name, tool_input)

    def build_reply_card(self, snapshot: ReplySnapshot) -> dict[str, Any]:
        """保留旧静态回复卡构建入口，兼容外部调用。"""
        return reply_card(snapshot.text, snapshot.state, snapshot.detail, snapshot.metrics, snapshot.final, snapshot.steps)
