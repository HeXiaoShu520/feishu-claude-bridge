from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .app.permissions import PermissionBroker
from .app.service import BotService, IncomingMessage, ReplySnapshot
from .app.store import ConversationStore
from .claude.agent import ClaudeAgent
from .lark.feishu import FeishuBot


# .env is local-only; secrets are never committed.
load_dotenv()


class _SafeSdkLogFilter(logging.Filter):
    """过滤飞书 SDK 连接日志，避免输出 URL 中的 access_key 和 ticket。"""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "processor not found, type: im.message.message_read_v1" in message:
            return False
        if record.name.lower().startswith(("lark", "lark_oapi")) and any(marker in message.lower() for marker in ("connected ws", "connected to ws", "access_key=", "ticket=")):
            return False
        return True


def _configure_safe_logging() -> None:
    """关闭飞书 SDK 的连接日志，避免输出带凭证的 WebSocket URL。"""
    safe_filter = _SafeSdkLogFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(safe_filter)
    for logger_name in ("Lark", "lark", "lark_oapi"):
        sdk_logger = logging.getLogger(logger_name)
        sdk_logger.setLevel(logging.WARNING)
        sdk_logger.propagate = False
        for handler in sdk_logger.handlers:
            handler.addFilter(safe_filter)


def require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


async def run() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _configure_safe_logging()
    cwd = Path(require("FEISHU_CLAUDE_CWD")).resolve()
    if not cwd.is_dir():
        raise RuntimeError(f"工作目录不存在：{cwd}")
    store = ConversationStore(Path(os.getenv("FEISHU_CLAUDE_DB_PATH", Path.home() / ".feishu-claude-mvp" / "sessions.db")))
    bot: FeishuBot
    permissions = PermissionBroker()

    def send_factory(message: IncomingMessage):
        return lambda text: bot.send_text(message.chat_id, text)

    class FeishuReplyStream:
        def __init__(self, message: IncomingMessage) -> None:
            self.chat_id = message.chat_id

        async def start(self, snapshot: ReplySnapshot):
            return await bot.create_streaming_reply(self.chat_id, snapshot)

        async def update(self, handle, snapshot: ReplySnapshot) -> None:
            await bot.update_streaming_reply(handle, snapshot)

    def permission_factory(message: IncomingMessage):
        async def ask(tool_name: str, tool_input: dict) -> str:
            async def send_card(approval_id: str, token: str, name: str, data: dict) -> str:
                return await bot.send_card(message.chat_id, bot.build_permission_card(approval_id, token, name, data))
            return await permissions.request(message.chat_id, message.user_open_id, send_card, tool_name, tool_input, bot.update_permission_message)
        return ask

    service = BotService(store, cwd, send_factory, FeishuReplyStream, permission_factory, ClaudeAgent)

    def on_card_action(value: dict[str, str], chat_id: str, user_open_id: str) -> bool:
        return permissions.resolve(value.get("approval_id", ""), value.get("token", ""), value.get("decision", ""), chat_id, user_open_id)

    bot = FeishuBot(require("FEISHU_APP_ID"), require("FEISHU_APP_SECRET"), service.handle_message, on_card_action)
    await asyncio.to_thread(bot.start, asyncio.get_running_loop())


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
