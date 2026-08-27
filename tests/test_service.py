"""服务层会话隔离、流式更新和 client 复用测试。"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.app.service import BotService, IncomingMessage, ReplySnapshot
from src.app.store import ConversationStore


@dataclass
class Metrics:
    """模拟 Agent SDK 的最终用量。"""

    model: str | None = "claude-test"
    input_tokens: int | None = 10
    output_tokens: int | None = 20
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    elapsed_seconds: float = 0.1
    tool_calls: int = 0


@dataclass
class AgentResult:
    """模拟一次 Claude 请求结果。"""

    session_id: str | None
    metrics: Metrics = field(default_factory=Metrics)


class FakeAgent:
    """可复用的 Claude client 替身。"""

    instances = []

    def __init__(self, cwd, mode, session_id):
        self.cwd, self.mode, self.session_id = cwd, mode, session_id
        self.interrupted = False
        self.closed = False
        FakeAgent.instances.append(self)

    def matches(self, cwd, mode, session_id):
        return self.cwd == cwd and self.mode == mode and self.session_id == session_id

    async def run(self, prompt, output, _permission, status=None):
        if status:
            status("正在回答", None)
        output(f"回复：{prompt}")
        await asyncio.sleep(0)
        self.session_id = "session-1"
        return AgentResult("session-1")

    async def interrupt(self):
        self.interrupted = True
        return True

    async def close(self):
        self.closed = True


class FakeReplyStream:
    """采集卡片快照而不访问飞书。"""

    def __init__(self, sent):
        self.sent = sent

    async def start(self, snapshot: ReplySnapshot):
        self.sent.append(("start", snapshot))
        return "card-1"

    async def update(self, message_id, snapshot: ReplySnapshot):
        self.sent.append(("update", message_id, snapshot))


@pytest.mark.asyncio
async def test_messages_keep_separate_sessions(tmp_path: Path) -> None:
    """不同群聊用户不能共享 Claude session。"""
    FakeAgent.instances.clear()
    sent, cards = [], []
    store = ConversationStore(tmp_path / "sessions.db")

    def send_factory(_message):
        async def send(text):
            sent.append(text)
        return send

    service = BotService(store, tmp_path, send_factory, lambda _message: FakeReplyStream(cards), lambda _message: _deny, FakeAgent)
    first, second = IncomingMessage("chat-a", "user-a", "你好", "message-a"), IncomingMessage("chat-a", "user-b", "你好", "message-b")
    await service.handle_message(first)
    await service.handle_message(second)
    await asyncio.sleep(0.3)

    assert first.conversation_key != second.conversation_key
    assert store.get(first.conversation_key).session_id == "session-1"
    assert store.get(second.conversation_key).session_id == "session-1"
    assert sent == []
    completed = [item[2] for item in cards if item[0] == "update" and item[2].final]
    assert len(completed) == 2
    assert {item.text for item in completed} == {"回复：你好"}
    assert all(item.metrics.output_tokens == 20 for item in completed)
    assert all(item.session_id == "session-1" for item in completed)


@pytest.mark.asyncio
async def test_same_conversation_reuses_agent(tmp_path: Path) -> None:
    """空闲的同会话 client 在下一条消息中复用。"""
    FakeAgent.instances.clear()
    store = ConversationStore(tmp_path / "sessions.db")
    service = BotService(store, tmp_path, lambda _message: _send, lambda _message: FakeReplyStream([]), lambda _message: _deny, FakeAgent)
    message = IncomingMessage("chat-a", "user-a", "第一次", "message-a")
    await service.handle_message(message)
    await asyncio.sleep(0.3)
    await service.handle_message(IncomingMessage("chat-a", "user-a", "第二次", "message-b"))
    await asyncio.sleep(0.3)

    assert len(FakeAgent.instances) == 1


async def _deny(_tool, _input):
    """拒绝测试中的工具请求。"""
    return "deny"


async def _send(_text):
    """丢弃测试中的普通文本回复。"""
