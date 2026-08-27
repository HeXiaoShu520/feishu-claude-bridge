from src.app.commands import HelpCommand, InvalidCommand, ModeCommand, NewCommand, ResumeCommand, StopCommand, TextPrompt, parse_command


def test_parse_supported_commands() -> None:
    assert isinstance(parse_command("/new"), NewCommand)
    assert isinstance(parse_command("/stop"), StopCommand)
    assert isinstance(parse_command("/help"), HelpCommand)
    assert parse_command("/resume abc").session_id == "abc"
    assert parse_command("/resume") == ResumeCommand(None)
    assert parse_command("/mode plan") == ModeCommand("plan")


def test_parse_text_and_rejects_bypass_permissions() -> None:
    assert parse_command("你好") == TextPrompt("你好")
    assert isinstance(parse_command("/mode bypassPermissions"), InvalidCommand)
