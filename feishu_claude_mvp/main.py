from __future__ import annotations

import asyncio
import logging
import os
import sys
import signal
from pathlib import Path

from dotenv import load_dotenv

from .app.permissions import PermissionBroker
from .app.service import BotService, IncomingMessage, ReplySnapshot
from .app.store import ConversationStore
from .claude.agent import ClaudeAgent
from .lark.feishu import FeishuBot


# .env is local-only; secrets are never committed.
load_dotenv()


class _ColorFormatter(logging.Formatter):
    """为日志时间、级别、输入输出和技能调用添加终端颜色。"""

    RESET = "\033[0m"
    COLORS = {"DEBUG": "\033[90m", "INFO": "\033[36m", "WARNING": "\033[33m", "ERROR": "\033[31m", "CRITICAL": "\033[1;31m"}

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if not sys.stderr.isatty():
            return message
        color = self.COLORS.get(record.levelname, self.RESET)
        if "收到飞书输入" in message:
            color = "\033[1;34m"
        elif "Claude 输入" in message:
            color = "\033[1;35m"
        elif "Claude 输出" in message or "更新飞书 CardKit" in message:
            color = "\033[1;32m"
        elif "技能" in message or "tool" in message.lower():
            color = "\033[1;33m"
        return f"{color}{message}{self.RESET}"


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
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
    formatter = _ColorFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
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
            self.message_id = message.message_id
            self.user_open_id = message.user_open_id

        async def start(self, snapshot: ReplySnapshot):
            return await bot.create_streaming_reply(self.message_id, snapshot, self.user_open_id)

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
        if value.get("action") in {"new", "details", "compact"}:
            if value.get("chat_id") != chat_id or value.get("user_open_id") != user_open_id:
                return False
            asyncio.create_task(service.handle_card_action(value["action"], chat_id, user_open_id))
            return True
        return permissions.resolve(value.get("approval_id", ""), value.get("token", ""), value.get("decision", ""), chat_id, user_open_id)

    bot = FeishuBot(require("FEISHU_APP_ID"), require("FEISHU_APP_SECRET"), service.handle_message, on_card_action)
    loop = asyncio.get_running_loop()
    shutting_down = False

    def request_shutdown() -> None:
        """同步请求 SDK 和 WebSocket 线程停止，确保 Ctrl+C 后不残留进程。"""
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        logging.getLogger(__name__).info("收到退出信号，正在终止全部子任务")
        bot.stop()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, request_shutdown)
        except (NotImplementedError, RuntimeError):
            signal.signal(signum, lambda _signum, _frame: request_shutdown())

    try:
        await asyncio.to_thread(bot.start, loop)
    finally:
        request_shutdown()


def main() -> None:
    """启动机器人，并在 Ctrl+C 时让异步任务和进程干净退出。"""
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("收到 Ctrl+C，程序已退出")


if __name__ == "__main__":
    main()
