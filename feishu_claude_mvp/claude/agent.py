"""Claude Agent SDK 适配层：流式事件、会话复用和运行指标。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..app.commands import VALID_MODES

log = logging.getLogger(__name__)
PermissionPrompt = Callable[[str, dict[str, Any]], Awaitable[bool]]
Output = Callable[[str], None]
Status = Callable[[str, str | None], None]


@dataclass
class RunMetrics:
    """记录一次 Claude 请求可安全展示的运行指标。"""

    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    elapsed_seconds: float = 0.0
    tool_calls: int = 0
    context_percentage: float | None = None
    context_tokens: int | None = None


@dataclass
class AgentResult:
    """Claude 请求完成后的会话标识和指标。"""

    session_id: str | None
    metrics: RunMetrics = field(default_factory=RunMetrics)


class ClaudeAgent:
    """围绕官方 Claude Agent SDK 的可复用流式客户端。"""

    def __init__(self, cwd: Path, mode: str, session_id: str | None = None) -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"unsupported permission mode: {mode}")
        self.cwd = cwd
        self.mode = mode
        self.session_id = session_id
        self._active_task: asyncio.Task[AgentResult] | None = None
        self._client: Any | None = None
        self._connected = False

    def matches(self, cwd: Path, mode: str, session_id: str | None) -> bool:
        """判断现有客户端能否安全复用于指定会话配置。"""
        return self.cwd == cwd and self.mode == mode and self.session_id == session_id

    async def run(self, prompt: str, output: Output, ask_permission: PermissionPrompt, status: Status | None = None) -> AgentResult:
        """执行一轮请求，并保留成功连接供同会话下一轮复用。"""
        if self._active_task and not self._active_task.done():
            raise RuntimeError("Claude is already processing a request")
        self._active_task = asyncio.create_task(self._run(prompt, output, ask_permission, status))
        try:
            return await self._active_task
        finally:
            self._active_task = None

    async def context_details(self) -> str | None:
        """读取当前 Claude Code 会话的真实上下文占用。"""
        if not self._client or not self._connected:
            return None
        try:
            usage = await self._client.get_context_usage()
        except Exception:
            log.debug("无法读取 Claude 上下文详情", exc_info=True)
            return None
        percentage = float(usage.get("percentage", 0.0))
        tokens = int(usage.get("totalTokens", 0))
        window = usage.get("contextWindow") or usage.get("context_window")
        window_text = f" / {int(window) / 1000:.1f}K" if window else ""
        return f"会话 {self.session_id or '未知'}\n上下文：{tokens / 1000:.1f}K{window_text}（{percentage:.1f}%）\n模型：{self._model_name()}\n工作目录：{self.cwd}"

    def _model_name(self) -> str:
        """返回当前 SDK 客户端记录的模型名。"""
        return getattr(self._client, "model", None) or "SDK 未提供"

    async def compact(self) -> bool:
        """向当前 Claude Code 会话发送上下文压缩指令。"""
        if not self._client or not self._connected:
            return False
        try:
            await self._client.query("/compact")
            async for _ in self._client.receive_response():
                pass
            return True
        except Exception:
            await self.close()
            raise

    async def interrupt(self) -> bool:
        """请求中断正在执行的 Claude 轮次。"""
        if not self._active_task or self._active_task.done():
            return False
        if self._client:
            await self._client.interrupt()
        return True

    async def close(self) -> None:
        """释放 Claude Code 子进程连接。"""
        if self._client:
            await self._client.disconnect()
        self._client = None
        self._connected = False

    async def _run(self, prompt: str, output: Output, ask_permission: PermissionPrompt, status: Status | None) -> AgentResult:
        """连接（如有需要）、发送请求并消费至 ResultMessage。"""
        try:
            from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
            from claude_agent_sdk.types import AssistantMessage, ResultMessage, StreamEvent
        except ImportError as exc:
            raise RuntimeError("缺少 claude-agent-sdk。请执行: python -m pip install -e '.[dev]'") from exc

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], _context: Any) -> Any:
            if status:
                status("等待授权", f"正在请求工具授权：{tool_name}")
            allowed = await ask_permission(tool_name, tool_input)
            from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
            if allowed:
                return PermissionResultAllow(updated_input=tool_input)
            return PermissionResultDeny(message="用户拒绝该工具调用")

        if not self._client:
            options_kwargs: dict[str, Any] = {"cwd": str(self.cwd), "permission_mode": self.mode, "include_partial_messages": True}
            if self.session_id:
                options_kwargs["resume"] = self.session_id
            if self.mode == "default":
                options_kwargs["can_use_tool"] = can_use_tool
            self._client = ClaudeSDKClient(options=ClaudeAgentOptions(**options_kwargs))

        started_at = time.monotonic()
        metrics = RunMetrics()
        latest_session_id = self.session_id
        partial_text = ""
        reused_client = self._connected
        if status:
            status("思考中", "正在继续当前会话…" if reused_client else "正在连接 Claude Code…")
        try:
            if not reused_client:
                await self._client.connect()
                self._connected = True
                log.info("Claude 会话已连接：是否恢复旧会话=%s", "是" if self.session_id else "否")
            log.info("Claude 请求开始：是否复用客户端=%s", "是" if reused_client else "否")
            log.info("Claude 输入：%s", prompt)
            await self._client.query(prompt)
            if status:
                status("思考中", None)
            async for message in self._client.receive_response():
                if isinstance(message, StreamEvent):
                    text = _text_delta(message)
                    if text:
                        partial_text += text
                        log.info("Claude 输出增量：%s", text)
                        output(text)
                        if status:
                            status("正在回答", None)
                    tool_name = _tool_name(message)
                    if tool_name:
                        metrics.tool_calls += 1
                        log.info("技能调用：%s", tool_name)
                        if status:
                            status("执行工具", f"正在执行：{tool_name}")
                elif isinstance(message, AssistantMessage):
                    full_text = "".join(_block_text(block) for block in getattr(message, "content", []))
                    if full_text.startswith(partial_text):
                        tail = full_text[len(partial_text):]
                    else:
                        tail = full_text
                    if tail:
                        partial_text += tail
                        log.info("Claude 输出文本：%s", tail)
                        output(tail)
                elif isinstance(message, ResultMessage):
                    latest_session_id = message.session_id or latest_session_id
                    metrics = _metrics_from_result(message, started_at)
                    if message.is_error:
                        detail = message.result or "; ".join(message.errors or []) or "未知错误"
                        raise RuntimeError(detail)
            try:
                context_usage = await self._client.get_context_usage()
                metrics.context_percentage = float(context_usage.get("percentage", 0.0))
                metrics.context_tokens = int(context_usage.get("totalTokens", 0))
            except Exception:
                log.debug("无法读取 Claude 上下文占用", exc_info=True)
            self.session_id = latest_session_id
            log.info("Claude 请求完成：会话=%s，模型=%s，输入Token=%s，输出Token=%s，耗时=%.2f秒", _short_id(latest_session_id), metrics.model, metrics.input_tokens, metrics.output_tokens, metrics.elapsed_seconds)
            return AgentResult(session_id=latest_session_id, metrics=metrics)
        except Exception:
            # 不复用故障连接；下一轮将以持久化 session_id 自动恢复。
            await self.close()
            raise


def _metrics_from_result(message: Any, started_at: float) -> RunMetrics:
    """从安装版 SDK 的 ResultMessage 提取公开 usage 字段。"""
    usage = getattr(message, "usage", None) or {}
    model_usage = getattr(message, "model_usage", None) or {}
    model = next(iter(model_usage), None)
    if model and isinstance(model_usage.get(model), dict):
        usage = {**model_usage[model], **usage}
    return RunMetrics(
        model=usage.get("canonicalModel") or model,
        input_tokens=_number(usage, "input_tokens", "inputTokens"),
        output_tokens=_number(usage, "output_tokens", "outputTokens"),
        cache_read_tokens=_number(usage, "cache_read_input_tokens", "cacheReadInputTokens"),
        cache_creation_tokens=_number(usage, "cache_creation_input_tokens", "cacheCreationInputTokens"),
        elapsed_seconds=(getattr(message, "duration_ms", 0) or 0) / 1000 or time.monotonic() - started_at,
    )


def _number(data: dict[str, Any], *names: str) -> int | None:
    """兼容 CLI snake_case 与 modelUsage camelCase 的整数 usage 字段。"""
    for name in names:
        value = data.get(name)
        if isinstance(value, int):
            return value
    return None


def _text_delta(message: Any) -> str:
    """提取 Anthropic 流式文本增量。"""
    event = getattr(message, "event", message)
    if _field(event, "type") != "content_block_delta":
        return ""
    delta = _field(event, "delta")
    return _field(delta, "text", "") if _field(delta, "type") == "text_delta" else ""


def _tool_name(message: Any) -> str | None:
    """从工具开始事件提取工具名，不暴露工具输入。"""
    event = getattr(message, "event", message)
    if _field(event, "type") != "content_block_start":
        return None
    block = _field(event, "content_block")
    return _field(block, "name") if _field(block, "type") == "tool_use" else None


def _block_text(block: Any) -> str:
    """提取 AssistantMessage 中的文本块。"""
    return _field(block, "text", "") if _field(block, "type") == "text" else ""


def _field(value: Any, name: str, default: Any = None) -> Any:
    """兼容字典和 SDK 对象的字段访问。"""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _short_id(value: str | None) -> str:
    """缩短会话标识以避免日志噪声。"""
    return value[:8] if value else "-"
