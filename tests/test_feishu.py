"""飞书 CardKit 流式卡片适配测试。"""

import json
from types import SimpleNamespace

import pytest

from feishu_claude_mvp.app.service import ReplySnapshot
from feishu_claude_mvp.lark.feishu import FeishuBot, compact_content


class Response:
    """模拟飞书 SDK 响应。"""

    def __init__(self, message_id: str = "message-1", card_id: str = "card-1", reaction_id: str = "reaction-1") -> None:
        self.code = 0
        self.msg = ""
        self.data = SimpleNamespace(message_id=message_id, card_id=card_id, reaction_id=reaction_id)

    def success(self) -> bool:
        """返回成功响应。"""
        return True


class Api:
    """记录 CardKit、消息回复和表情请求。"""

    def __init__(self) -> None:
        self.creates, self.replies, self.contents, self.settings, self.reactions, self.deleted = [], [], [], [], [], []

    def create_card(self, request):
        self.creates.append(request)
        return Response()

    def reply(self, request):
        self.replies.append(request)
        return Response()

    def content(self, request):
        self.contents.append(request)
        return Response()

    def settings_update(self, request):
        self.settings.append(request)
        return Response()

    def reaction_create(self, request):
        self.reactions.append(request)
        return Response()

    def reaction_delete(self, request):
        self.deleted.append(request)
        return Response()



def make_bot(api: Api) -> FeishuBot:
    """创建注入 fake SDK 客户端的机器人。"""
    bot = FeishuBot("app", "secret", lambda _message: None, lambda _value, _chat, _user: False)
    bot.client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(reply=api.reply), message_reaction=SimpleNamespace(create=api.reaction_create, delete=api.reaction_delete))),
        cardkit=SimpleNamespace(v1=SimpleNamespace(card=SimpleNamespace(create=api.create_card, settings=api.settings_update), card_element=SimpleNamespace(content=api.content))),
    )
    return bot


@pytest.mark.asyncio
async def test_cardkit_streaming_reply_uses_entity_reply_and_sequenced_updates() -> None:
    """流式回复创建实体、回复引用并递增更新序号。"""
    api = Api()
    bot = make_bot(api)
    handle = await bot.create_streaming_reply("incoming-1", ReplySnapshot("", "思考中", "准备请求…"))
    await bot.update_streaming_reply(handle, ReplySnapshot("逐步输出", "正在回答", "生成正文"))
    await bot.update_streaming_reply(handle, ReplySnapshot("最终输出", "已完成", metrics=None, final=True))

    card = json.loads(api.creates[0].request_body.data)
    assert card["schema"] == "2.0"
    assert card["config"]["streaming_mode"] is True
    assert card["body"]["elements"][0]["element_id"] == "stream_md"
    assert api.replies[0].message_id == "incoming-1"
    assert json.loads(api.replies[0].request_body.content)["data"]["card_id"] == "card-1"
    assert [request.request_body.sequence for request in api.contents] == [1, 2]
    assert len(api.settings) == 1
    assert json.loads(api.settings[0].request_body.settings)["config"]["streaming_mode"] is False
    assert not api.reactions
    assert not api.deleted
    assert handle.card_id == "card-1"


def test_compact_content_appends_metrics_once() -> None:
    """终态 CardKit 正文只能出现一条模型消耗后缀。"""
    metrics = SimpleNamespace(model="claude-test", input_tokens=12, output_tokens=34, cache_read_tokens=5, cache_creation_tokens=2, elapsed_seconds=1.25)
    content = compact_content(ReplySnapshot("最终结果", "已完成", metrics=metrics, final=True, session_id="session-1"))

    assert content.count("claude-test") == 1
    assert content.count("输入 0.0K / 输出 0.0K") == 1


@pytest.mark.asyncio
async def test_cardkit_streaming_reply_does_not_use_message_patch() -> None:
    """CardKit 流式路径只依赖 reply、element content 和 settings。"""
    api = Api()
    bot = make_bot(api)
    await bot.create_streaming_reply("incoming-1", ReplySnapshot("", "思考中"))
    assert not hasattr(bot.client.im.v1.message, "patch")
