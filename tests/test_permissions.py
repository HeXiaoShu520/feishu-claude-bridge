import asyncio

import pytest

from feishu_claude_mvp.app.permissions import PermissionBroker


@pytest.mark.asyncio
async def test_permission_requires_original_user_and_can_only_resolve_once() -> None:
    broker, cards = PermissionBroker(), []

    async def send_card(approval_id, token, *_args):
        cards.extend([approval_id, token])

    waiting = asyncio.create_task(broker.request("chat", "user", send_card, "Bash", {"command": "pwd"}))
    await asyncio.sleep(0)
    approval_id, token = cards
    assert not broker.resolve(approval_id, token, "allow_once", "chat", "other-user")
    assert broker.resolve(approval_id, token, "allow_once", "chat", "user")
    assert not broker.resolve(approval_id, token, "allow_once", "chat", "user")
    assert await waiting == "allow_once"
