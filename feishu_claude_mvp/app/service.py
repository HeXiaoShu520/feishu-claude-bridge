"""飞书消息到 Claude 会话的编排、流式卡片更新和会话隔离。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .commands import HelpCommand, InvalidCommand, ModeCommand, NewCommand, ResumeCommand, StopCommand, TextPrompt, parse_command
from .store import Conversation, ConversationStore


class AgentResult(Protocol):
    """服务层需要的 Claude 请求结果。"""

    session_id: str | None
    metrics: Any


class Agent(Protocol):
    """服务层使用的 Claude 客户端边界。"""

    def matches(self, cwd: Path, mode: str, session_id: str | None) -> bool: ...
    async def run(self, prompt: str, output, ask_permission, status=None) -> AgentResult: ...
    async def interrupt(self) -> bool: ...
    async def close(self) -> None: ...


@dataclass(frozen=True)
class ReplySnapshot:
    """一次卡片渲染所需的内容和运行状态。"""

    text: str
    state: str
    detail: str | None = None
    metrics: Any = None
    final: bool = False
    steps: tuple[str, ...] = ()
    session_id: str | None = None
    permission: dict[str, Any] | None = None


class ReplyStream(Protocol):
    """面向消息平台的回复卡片接口。"""

    async def start(self, snapshot: ReplySnapshot) -> Any: ...
    async def update(self, handle: Any, snapshot: ReplySnapshot) -> None: ...


SendText = Callable[[str], Awaitable[None]]
AskPermission = Callable[[str, dict[str, Any]], Awaitable[str]]
AgentFactory = Callable[[Path, str, str | None], Agent]
ReplyStreamFactory = Callable[["IncomingMessage"], ReplyStream]


@dataclass(frozen=True)
class IncomingMessage:
    """归一化后的飞书文本消息。"""

    chat_id: str
    user_open_id: str
    text: str
    message_id: str

    @property
    def conversation_key(self) -> str:
        """按群聊和用户隔离 Claude 上下文。"""
        return f"{self.chat_id}:{self.user_open_id}"


class ConversationController:
    """管理一个飞书用户会话的单任务执行与 Claude client 复用。"""

    def __init__(self, conversation: Conversation, store: ConversationStore, send_text: SendText, reply_stream: ReplyStream, ask_permission: AskPermission, agent_factory: AgentFactory) -> None:
        self.conversation = conversation
        self.store = store
        self.send_text = send_text
        self.reply_stream = reply_stream
        self.ask_permission = ask_permission
        self.agent_factory = agent_factory
        self._lock = asyncio.Lock()
        self._agent: Agent | None = None
        self._task: asyncio.Task[None] | None = None
        self._generation = 0

    async def handle(self, text: str) -> None:
        """解析命令或启动新的普通对话任务。"""
        command = parse_command(text)
        async with self._lock:
            if isinstance(command, TextPrompt):
                await self._replace_task(command.text)
            elif isinstance(command, StopCommand):
                await self._stop(keep_agent=True)
                await self.send_text("已请求停止当前任务。")
            elif isinstance(command, HelpCommand):
                await self.send_text(_help_text())
            elif isinstance(command, NewCommand):
                await self._stop()
                self.store.clear_session(self.conversation.key)
                self.conversation = Conversation(self.conversation.key, None, self.conversation.mode, self.conversation.cwd, self.conversation.chat_id, self.conversation.user_open_id)
                self.store.save(self.conversation)
                await self.send_text("已创建新会话。")
            elif isinstance(command, ResumeCommand):
                await self._stop()
                session_id = command.session_id or self.conversation.session_id
                if session_id and len(session_id) == 8:
                    session_id = self.store.find_session_by_suffix(session_id, self.conversation.chat_id, self.conversation.user_open_id)
                if not session_id:
                    await self.send_text("没有可恢复的会话。")
                    return
                self.conversation = Conversation(self.conversation.key, session_id, self.conversation.mode, self.conversation.cwd, self.conversation.chat_id, self.conversation.user_open_id)
                self.store.save(self.conversation)
                await self.send_text("已设置为恢复已有 Claude 会话。")
            elif isinstance(command, ModeCommand):
                await self._stop()
                self.conversation = Conversation(self.conversation.key, self.conversation.session_id, command.mode, self.conversation.cwd, self.conversation.chat_id, self.conversation.user_open_id)
                self.store.save(self.conversation)
                await self.send_text(f"权限模式已切换为 {command.mode}。")
            elif isinstance(command, InvalidCommand):
                await self.send_text(command.message)

    async def _replace_task(self, prompt: str) -> None:
        """中断旧任务后启动新任务，保证新消息优先。"""
        await self._stop(keep_agent=True)
        self._generation += 1
        self._task = asyncio.create_task(self._run(prompt, self._generation))

    async def _stop(self, keep_agent: bool = False) -> None:
        """停止活动请求，并按配置保留或释放空闲 client。"""
        if self._agent:
            await self._agent.interrupt()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=30)
            except asyncio.TimeoutError:
                await self.send_text("停止超时，旧任务不会再发送回复。")
        if not keep_agent and self._agent:
            await self._agent.close()
            self._agent = None

    def _get_agent(self) -> Agent:
        """返回与当前会话匹配的常驻客户端，否则重建。"""
        cwd = Path(self.conversation.cwd)
        if self._agent and self._agent.matches(cwd, self.conversation.mode, self.conversation.session_id):
            return self._agent
        self._agent = self.agent_factory(cwd, self.conversation.mode, self.conversation.session_id)
        return self._agent

    async def _run(self, prompt: str, generation: int) -> None:
        """运行一轮 Claude 请求并将文本和状态渐进渲染到飞书。"""
        agent = self._get_agent()
        parts: list[str] = []
        changed = asyncio.Event()
        finished = asyncio.Event()
        state = "思考中"
        detail: str | None = "准备请求…"
        metrics: Any = None
        session_id: str | None = None
        steps: list[str] = [detail]
        reply_handle = await self.reply_stream.start(ReplySnapshot("", state, detail, steps=tuple(steps)))
        updater = asyncio.create_task(self._update_reply(reply_handle, parts, changed, finished, generation, lambda: ReplySnapshot("".join(parts), state, detail, metrics, False, tuple(steps))))

        def output(text: str) -> None:
            if generation == self._generation:
                parts.append(text)
                changed.set()

        def status(next_state: str, next_detail: str | None) -> None:
            nonlocal state, detail
            if generation == self._generation:
                state, detail = next_state, next_detail
                step = next_detail or _state_summary(next_state)
                if step and (not steps or steps[-1] != step):
                    steps[:] = [*steps[-2:], step]
                changed.set()

        async def permission(tool_name: str, tool_input: dict[str, Any]) -> bool:
            if self.store.has_permission(self.conversation.key, tool_name):
                return True
            decision = await self.ask_permission(tool_name, tool_input)
            if decision == "allow_session":
                self.store.grant_permission(self.conversation.key, tool_name)
                return True
            return decision == "allow_once"

        final_state = "已完成"
        try:
            result = await agent.run(prompt, output, permission, status)
            metrics = result.metrics
            session_id = result.session_id
            if generation != self._generation:
                final_state = "已停止"
                return
            if result.session_id:
                self.conversation = Conversation(self.conversation.key, result.session_id, self.conversation.mode, self.conversation.cwd, self.conversation.chat_id, self.conversation.user_open_id)
                self.store.save(self.conversation)
        except Exception as exc:
            final_state = "执行失败"
            if generation == self._generation:
                parts.append(f"Claude 执行失败：{exc}")
        finally:
            finished.set()
            changed.set()
            try:
                await updater
                if generation == self._generation:
                    final_snapshot = ReplySnapshot("".join(parts) or "任务已结束。", final_state, None, metrics, True, session_id=session_id)
                    await self.reply_stream.update(reply_handle, final_snapshot)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("飞书回复卡片更新失败：状态=%s", final_state)

    async def _update_reply(self, reply_handle: Any, parts: list[str], changed: asyncio.Event, finished: asyncio.Event, generation: int, snapshot: Callable[[], ReplySnapshot]) -> None:
        """将完整增量内容交给飞书原生流式卡片渲染。"""
        last_sent = ""
        first_update = True
        while generation == self._generation and not finished.is_set():
            await changed.wait()
            changed.clear()
            if not first_update:
                await asyncio.sleep(0.07)
            first_update = False
            current = snapshot()
            key = f"{current.state}\0{current.detail}\0{current.text}"
            if key != last_sent and not finished.is_set():
                try:
                    await self.reply_stream.update(reply_handle, current)
                    last_sent = key
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception("飞书流式卡片更新失败：状态=%s，正文长度=%d", current.state, len(current.text))
                    return


def _state_summary(state: str) -> str | None:
    """将状态映射为用户可见的简短过程摘要。"""
    return {"思考中": "正在分析请求", "正在回答": "正在生成答复"}.get(state)


def _help_text() -> str:
    """返回机器人本地命令说明，不转交 Claude。"""
    return """可用命令：
/help：显示本帮助
/new：新建 Claude 会话并清除本会话授权
/stop：停止当前任务
/resume [session_id]：恢复当前或指定会话
/mode default|acceptEdits|plan|dontAsk：切换权限模式

直接发送文字即可继续当前 Claude 会话。"""


class BotService:
    """为每个飞书会话创建和缓存 ConversationController。"""

    def __init__(self, store: ConversationStore, cwd: Path, send_factory: Callable[[IncomingMessage], SendText], reply_stream_factory: ReplyStreamFactory, permission_factory: Callable[[IncomingMessage], AskPermission], agent_factory: AgentFactory) -> None:
        self.store = store
        self.cwd = str(cwd.resolve())
        self.send_factory = send_factory
        self.reply_stream_factory = reply_stream_factory
        self.permission_factory = permission_factory
        self.agent_factory = agent_factory
        self.controllers: dict[str, ConversationController] = {}

    async def handle_message(self, message: IncomingMessage) -> None:
        """路由一条飞书消息到对应的会话控制器。"""
        controller = self.controllers.get(message.conversation_key)
        if not controller:
            conversation = self.store.get(message.conversation_key) or Conversation(message.conversation_key, None, "default", self.cwd, message.chat_id, message.user_open_id)
            controller = ConversationController(conversation, self.store, self.send_factory(message), self.reply_stream_factory(message), self.permission_factory(message), self.agent_factory)
            self.controllers[message.conversation_key] = controller
        await controller.handle(message.text)
