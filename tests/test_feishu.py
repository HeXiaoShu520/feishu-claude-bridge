"""飞书 CardKit 流式卡片适配测试。"""

import json
from types import SimpleNamespace

import pytest

from feishu_claude_mvp.app.service import ReplySnapshot
from feishu_claude_mvp.lark.feishu import FeishuBot


class Response:
    """模拟飞书 SDK 响应。"""

    def __init__(self, success: bool = True, message_id: str = "message-1", card_id: str = "card-1") -> None:
        self._success = success
        self.code = 999
        self.msg = "failed"
        self.data = SimpleNamespace(message_id=message_id, card_id=card_id)

    def success(self) -> bool:
        """返回模拟调用是否成功。"""
        return self._success


class CardApi:
    """采集 CardKit 创建、元素更新和配置更新请求。"""

    def __init__(self) -> None:
        self.creates = []
        self.contents = []
        self.settings = []
        self.updates = []
        self.create_response = Response()
        self.content_response = Response()
        self.settings_response = Response()
        self.update_response = Response()

    def create(self, request):
        """记录卡片实体创建请求。"""
        self.creates.append(request)
        return self.create_response

    def content(self, request):
        """记录卡片元素内容更新请求。"""
        self.contents.append(request)
        return self.content_response

    def settings_update(self, request):
        """记录卡片配置更新请求。"""
        self.settings.append(request)
        return self.settings_response

    def update(self, request):
        """记录整卡更新请求。"""
        self.updates.append(request)
        return self.update_response


class MessageApi:
    """采集发送卡片引用消息请求。"""

    def __init__(self) -> None:
        self.creates = []
        self.patches = []
        self.response = Response()

    def create(self, request):
        """记录消息创建请求。"""
        self.creates.append(request)
        return self.response

    def patch(self, request):
        """记录旧版消息 Patch 请求。"""
        self.patches.append(request)
        return self.response


def make_bot(card_api: CardApi, message_api: MessageApi) -> FeishuBot:
    """创建注入 CardKit 和 IM fake client 的机器人。"""
    bot = FeishuBot("app", "secret", lambda _message: None, lambda _value, _chat, _user: False)
    bot.client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=message_api)),
        cardkit=SimpleNamespace(v1=SimpleNamespace(card=SimpleNamespace(create=card_api.create, update=card_api.update, settings=card_api.settings_update), card_element=SimpleNamespace(content=card_api.content))),
    )
    return bot


@pytest.mark.asyncio
async def test_cardkit_streaming_reply_uses_entity_and_sequenced_element_updates() -> None:
    """流式回复创建卡片实体，并使用递增序号更新元素。"""
    card_api, message_api = CardApi(), MessageApi()
    bot = make_bot(card_api, message_api)
    handle = await bot.create_streaming_reply("chat-1", ReplySnapshot("", "思考中", "准备请求…"))
    await bot.update_streaming_reply(handle, ReplySnapshot("逐步输出", "正在回答", "生成正文"))
    await bot.update_streaming_reply(handle, ReplySnapshot("最终输出", "已完成", metrics=None, final=True))

    assert handle.message_id == "message-1"
    assert handle.card_id == ""
    assert len(message_api.creates) == 1
    assert len(message_api.patches) == 2
    assert json.loads(message_api.patches[0].request_body.content)["schema"] == "2.0"
    assert json.loads(message_api.patches[1].request_body.content)["config"]["streaming_mode"] is False


@pytest.mark.asyncio
async def test_cardkit_streaming_reply_requires_cardkit_api() -> None:
    """没有 CardKit API 时直接报告配置错误。"""
    bot = FeishuBot("app", "secret", lambda _message: None, lambda _value, _chat, _user: False)
    bot.client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=MessageApi())))

    handle = await bot.create_streaming_reply("chat-1", ReplySnapshot("", "思考中"))
    assert handle.message_id == "message-1"
