import asyncio
from pathlib import Path

from src.cli import TerminalApp
from src.app.store import Conversation, ConversationStore


def test_new_clears_session_and_mode_persists(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "sessions.db")
    app = TerminalApp(tmp_path, store)
    asyncio.run(app.handle("/mode plan"))
    app.conversation = Conversation("terminal", "session-1", "plan", str(tmp_path.resolve()))
    store.save(app.conversation)
    asyncio.run(app.handle("/new"))
    assert app.conversation.session_id is None
    assert app.conversation.mode == "plan"


def test_resume_replaces_session_id(tmp_path: Path) -> None:
    app = TerminalApp(tmp_path, ConversationStore(tmp_path / "sessions.db"))
    asyncio.run(app.handle("/resume sdk-session-1"))
    assert app.conversation.session_id == "sdk-session-1"
