"""飞书静态与原生流式卡片渲染测试。"""

from dataclasses import dataclass

from feishu_claude_mvp.app.service import ReplySnapshot
from feishu_claude_mvp.lark.cards import permission_card, reply_card, streaming_card


@dataclass
class Metrics:
    """提供卡片指标渲染所需字段。"""

    model: str = "claude-test"
    input_tokens: int = 12
    output_tokens: int = 34
    cache_read_tokens: int = 5
    cache_creation_tokens: int = 2
    elapsed_seconds: float = 1.25
    tool_calls: int = 1


def test_final_reply_card_contains_metrics() -> None:
    """静态终态卡片展示模型、Token 和耗时。"""
    card = reply_card("结果", "已完成", metrics=Metrics(), final=True)
    metrics_text = card["elements"][-1]["elements"][0]["content"]

    assert card["header"]["template"] == "green"
    assert "claude-test" in metrics_text
    assert "输入 12 / 输出 34" in metrics_text
    assert "1.2s" in metrics_text


def test_streaming_card_always_contains_body() -> None:
    """初始空内容帧也必须包含飞书要求的 body。"""
    card = streaming_card(ReplySnapshot("", "思考中", "准备请求…"))

    assert card["body"]["elements"]
    assert card["body"]["elements"][0]["content"] == "思考中"


def test_streaming_card_renders_complete_interactive_frames() -> None:
    """普通 interactive 卡片的每一帧都可独立渲染。"""
    running = streaming_card(ReplySnapshot("正在输出", "正在回答", "生成正文", steps=("准备请求…", "生成正文")))
    completed = streaming_card(ReplySnapshot("最终结果", "已完成", metrics=Metrics(), final=True, steps=("正在生成答复",), session_id="session-1"))

    assert running["schema"] == "2.0"
    assert running["config"]["streaming_mode"] is True
    assert running["body"]["elements"] == [{"tag": "markdown", "content": "正在输出"}]
    assert completed["config"]["streaming_mode"] is False
    assert completed["header"]["template"] == "green"
    assert [element["tag"] for element in completed["body"]["elements"]] == ["markdown"]
    assert "claude-test" in completed["body"]["elements"][0]["content"]
    assert "ession-1" in completed["body"]["elements"][0]["content"]


def test_markdown_compacts_blank_lines_but_keeps_code() -> None:
    """普通空行压缩，围栏代码块内部保持不变。"""
    card = reply_card("第一段\n\n\n第二段\n```python\n\n  x = 1\n\n```", "已完成")
    content = card["elements"][0]["content"]

    assert "第一段\n第二段" in content
    assert "```python\n\n  x = 1\n\n```" in content


def test_permission_card_redacts_sensitive_values() -> None:
    """授权卡片不能展示嵌套密钥。"""
    card = permission_card("approval", "token", "Bash", {"headers": {"Authorization": "secret"}, "api_key": "another-secret"})
    content = card["elements"][0]["content"]

    assert "secret" not in content
    assert "another-secret" not in content
    assert "***" in content
