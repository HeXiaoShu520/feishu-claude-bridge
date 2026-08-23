from pathlib import Path

from feishu_claude_mvp.app.store import Conversation, ConversationStore


def test_store_round_trip(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "sessions.db")
    conversation = Conversation("terminal", "session-1", "default", "C:/work")
    store.save(conversation)
    assert store.get("terminal") == conversation


def test_store_updates_existing_conversation(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "sessions.db")
    store.save(Conversation("terminal", "old", "default", "C:/work"))
    store.save(Conversation("terminal", "new", "plan", "C:/work"))
    assert store.get("terminal") == Conversation("terminal", "new", "plan", "C:/work")
