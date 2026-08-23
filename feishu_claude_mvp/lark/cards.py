"""飞书交互卡片：原生流式回复、运行指标和工具授权。"""

from __future__ import annotations

import json
from typing import Any

SENSITIVE_KEYS = {"token", "secret", "password", "authorization", "api_key", "apikey", "credential", "cookie", "key"}
STATE_TEMPLATES = {"建立会话": "blue", "思考中": "blue", "正在回答": "blue", "执行工具": "orange", "等待授权": "orange", "已完成": "green", "已停止": "grey", "执行失败": "red"}


def permission_card(approval_id: str, token: str, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """构造脱敏后的工具授权卡片。"""
    summary = json.dumps(_redact(tool_input), ensure_ascii=False, sort_keys=True)[:800]
    actions = [{"tag": "button", "text": {"tag": "plain_text", "content": text}, "type": style, "value": {"approval_id": approval_id, "token": token, "decision": decision}} for text, decision, style in [("允许一次", "allow_once", "primary"), ("本会话允许", "allow_session", "default"), ("拒绝", "deny", "danger")]]
    return {"config": {"wide_screen_mode": True}, "header": {"title": {"tag": "plain_text", "content": "Claude 请求工具授权"}, "template": "orange"}, "elements": [{"tag": "markdown", "content": f"**工具：** `{tool_name}`\\n\\n```json\\n{summary}\\n```"}, {"tag": "action", "actions": actions}]}


def permission_result_card(tool_name: str, tool_input: dict[str, Any], decision: str) -> dict[str, Any]:
    """构造显示授权结果且不再包含操作按钮的卡片。"""
    summary = json.dumps(_redact(tool_input), ensure_ascii=False, sort_keys=True)[:800]
    result_text = {"allow_once": "✅ 已授权一次", "allow_session": "✅ 本会话已授权", "deny": "❌ 已拒绝"}.get(decision, "⏱ 授权已超时")
    template = "green" if decision in {"allow_once", "allow_session"} else "red"
    return {"config": {"wide_screen_mode": True}, "header": {"title": {"tag": "plain_text", "content": f"Claude {result_text}"}, "template": template}, "elements": [{"tag": "markdown", "content": f"**工具：** `{tool_name}`\n\n```json\n{summary}\n```\n\n{result_text}"}]}


def streaming_card(snapshot: Any) -> dict[str, Any]:
    """渲染普通 interactive 消息每次 Patch 所需的完整流式卡片。"""
    elements = []
    if snapshot.text:
        elements.append({"tag": "markdown", "content": compact_markdown(snapshot.text)})
    title = "Claude"
    card = {"schema": "2.0", "config": {"streaming_mode": not snapshot.final, "update_multi": True, "summary": {"content": "Claude 正在生成回复"}, "streaming_config": {"print_frequency_ms": {"default": 70, "android": 70, "ios": 70, "pc": 70}, "print_step": {"default": 1, "android": 1, "ios": 1, "pc": 1}, "print_strategy": "fast"}}, "header": {"title": {"tag": "plain_text", "content": title}, "template": STATE_TEMPLATES.get(snapshot.state, "blue")}}
    if not elements:
        elements.append({"tag": "markdown", "content": stream_status(snapshot.state, getattr(snapshot, "detail", None), getattr(snapshot, "steps", ()))})
    if snapshot.permission:
        auth = permission_card(snapshot.permission["approval_id"], snapshot.permission["token"], snapshot.permission["tool_name"], snapshot.permission["tool_input"])
        elements.extend(auth["elements"])
    if snapshot.final and elements[0]["tag"] == "markdown":
        metrics = stream_metrics(snapshot.metrics, snapshot.session_id)
        elements[0]["content"] = f"{elements[0]['content']}\n\n<font color='grey'>{metrics}</font>"
    card["body"] = {"elements": elements}
    return card


def stream_status(state: str, detail: str | None, steps: tuple[str, ...] = ()) -> str:
    """只渲染当前状态，不展示历史过程。"""
    return state


def stream_metrics(metrics: Any, session_id: str | None = None) -> str:
    """渲染模型、短会话 ID 和终态指标。"""
    return _metrics_text(metrics, session_id)


def reply_card(text: str, state: str = "思考中", detail: str | None = None, metrics: Any = None, final: bool = False, steps: tuple[str, ...] = ()) -> dict[str, Any]:
    """保留静态卡片渲染，供非流式回退场景使用。"""
    elements = [{"tag": "markdown", "content": compact_markdown(text)}] if text else [{"tag": "markdown", "content": stream_status(state, detail, steps)}]
    if final:
        elements.extend([{"tag": "hr"}, {"tag": "note", "elements": [{"tag": "plain_text", "content": _metrics_text(metrics)}]}])
    return {"config": {"wide_screen_mode": True}, "header": {"title": {"tag": "plain_text", "content": f"Claude · {state}"}, "template": STATE_TEMPLATES.get(state, "blue")}, "elements": elements}


def _metrics_text(metrics: Any, session_id: str | None = None) -> str:
    """渲染最终任务指标，模型和会话短 ID 放在同一行。"""
    if metrics is None:
        return f"ID {session_id[-8:]}" if session_id else "Token：SDK 未提供"
    model = getattr(metrics, "model", None) or "SDK 未提供"
    input_tokens, output_tokens = getattr(metrics, "input_tokens", None), getattr(metrics, "output_tokens", None)
    token_text = "Token：SDK 未提供" if input_tokens is None and output_tokens is None else f"输入 {input_tokens or 0} / 输出 {output_tokens or 0}"
    cache_read, cache_written = getattr(metrics, "cache_read_tokens", None), getattr(metrics, "cache_creation_tokens", None)
    cache_text = "" if cache_read is None and cache_written is None else f" · 缓存 {cache_read or 0}/{cache_written or 0}"
    short_id = f" · {session_id[-8:]}" if session_id else ""
    return f"{model} · 输入 {input_tokens or 0} / 输出 {output_tokens or 0}{cache_text} · {getattr(metrics, 'elapsed_seconds', 0.0):.1f}s{short_id}"


def compact_markdown(text: str) -> str:
    """移除围栏外空行以压缩卡片高度，代码块内容完全保留。"""
    lines, compacted, in_fence = text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), [], False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            compacted.append(line)
        elif in_fence:
            compacted.append(line)
        elif line.strip():
            compacted.append(line.rstrip())
    return "\n".join(compacted).strip()


def _redact(value: Any) -> Any:
    """递归隐藏工具输入中的机密字段。"""
    if isinstance(value, dict):
        return {key: "***" if any(secret in key.lower() for secret in SENSITIVE_KEYS) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value[:20]]
    return value
