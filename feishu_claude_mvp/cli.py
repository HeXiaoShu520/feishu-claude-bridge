from __future__ import annotations

import asyncio
import json
import shlex
import sys
from pathlib import Path

from .app.commands import VALID_MODES
from .app.store import Conversation, ConversationStore
from .claude.agent import ClaudeAgent


SENSITIVE_KEYS = {"token", "secret", "password", "authorization", "api_key", "apikey", "credential", "cookie", "key"}


def _redact(value: object) -> object:
    """递归脱敏终端授权提示中的敏感字段。"""
    if isinstance(value, dict):
        return {key: "***" if any(secret in str(key).lower() for secret in SENSITIVE_KEYS) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value[:20]]
    return value


HELP = """命令：
  /new                 新建 Claude 会话
  /resume [session_id] 恢复保存的会话，或指定 Claude session ID
  /stop                中断当前任务
  /mode <mode>         default | acceptEdits | plan | dontAsk | bypassPermissions
  /status              显示当前会话
  /quit                退出
其他文本会直接发送给 Claude。"""


class TerminalApp:
    def __init__(self, cwd: Path, store: ConversationStore, key: str = "terminal") -> None:
        self.cwd = cwd.resolve()
        self.store = store
        self.key = key
        self.conversation = store.get(key) or Conversation(key, None, "default", str(self.cwd))
        self.agent: ClaudeAgent | None = None

    async def handle(self, line: str) -> bool:
        if line == "/quit":
            return False
        if line == "/help":
            print(HELP)
            return True
        if line == "/new":
            self.conversation = Conversation(self.key, None, self.conversation.mode, str(self.cwd))
            self.store.save(self.conversation)
            self.agent = None
            print("已创建新会话。")
            return True
        if line.startswith("/resume"):
            parts = shlex.split(line)
            session_id = parts[1] if len(parts) > 1 else self.conversation.session_id
            if not session_id:
                print("没有可恢复的会话。")
            else:
                self.conversation = Conversation(self.key, session_id, self.conversation.mode, str(self.cwd))
                self.store.save(self.conversation)
                self.agent = None
                print(f"将恢复会话: {session_id}")
            return True
        if line == "/status":
            print(json.dumps(self.conversation.__dict__, ensure_ascii=False, indent=2))
            return True
        if line == "/stop":
            if self.agent and await self.agent.interrupt():
                print("已请求中断。")
            else:
                print("当前没有运行中的任务。")
            return True
        if line.startswith("/mode "):
            mode = line.split(maxsplit=1)[1]
            if mode not in VALID_MODES:
                print(f"无效模式。可选：{', '.join(sorted(VALID_MODES))}")
            else:
                self.conversation = Conversation(self.key, self.conversation.session_id, mode, str(self.cwd))
                self.store.save(self.conversation)
                self.agent = None
                print(f"权限模式已改为 {mode}。")
            return True
        await self.send(line)
        return True

    async def send(self, prompt: str) -> None:
        if not self.agent:
            self.agent = ClaudeAgent(self.cwd, self.conversation.mode, self.conversation.session_id)
        print("Claude: ", end="", flush=True)
        try:
            result = await self.agent.run(prompt, self._write, self._ask_permission)
        except asyncio.CancelledError:
            print("\n任务已中断。")
            return
        except Exception as exc:
            print(f"\n[错误] {exc}")
            return
        print()
        if result.session_id:
            self.conversation = Conversation(self.key, result.session_id, self.conversation.mode, str(self.cwd))
            self.store.save(self.conversation)
            self.agent.session_id = result.session_id

    @staticmethod
    def _write(text: str) -> None:
        print(text, end="", flush=True)

    @staticmethod
    async def _ask_permission(tool_name: str, tool_input: dict[str, object]) -> bool:
        print(f"\n[需要授权] 工具：{tool_name}\n参数：{json.dumps(_redact(tool_input), ensure_ascii=False)[:500]}")
        answer = await asyncio.to_thread(input, "允许？[y/N] ")
        return answer.strip().lower() in {"y", "yes"}


async def _run(cwd: Path) -> None:
    store = ConversationStore(Path.home() / ".feishu-claude-mvp" / "sessions.db")
    app = TerminalApp(cwd, store)
    print("Feishu-Claude MVP（终端模式）。输入 /help 查看命令。")
    while True:
        try:
            line = await asyncio.to_thread(input, "> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not await app.handle(line.strip()):
            return


def main() -> None:
    cwd = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    asyncio.run(_run(cwd))


if __name__ == "__main__":
    main()
