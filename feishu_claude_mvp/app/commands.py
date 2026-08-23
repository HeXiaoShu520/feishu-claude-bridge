from __future__ import annotations

from dataclasses import dataclass

VALID_MODES = {"default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"}


@dataclass(frozen=True)
class TextPrompt:
    text: str


@dataclass(frozen=True)
class NewCommand:
    pass


@dataclass(frozen=True)
class StopCommand:
    pass


@dataclass(frozen=True)
class HelpCommand:
    pass


@dataclass(frozen=True)
class ResumeCommand:
    session_id: str | None


@dataclass(frozen=True)
class ModeCommand:
    mode: str


@dataclass(frozen=True)
class InvalidCommand:
    message: str


Command = TextPrompt | NewCommand | StopCommand | HelpCommand | ResumeCommand | ModeCommand | InvalidCommand


def parse_command(text: str) -> Command:
    text = text.strip()
    if not text.startswith("/"):
        return TextPrompt(text)

    command, _, argument = text.partition(" ")
    argument = argument.strip()
    if command == "/new":
        return NewCommand() if not argument else InvalidCommand("/new 不接受参数")
    if command == "/stop":
        return StopCommand() if not argument else InvalidCommand("/stop 不接受参数")
    if command == "/help":
        return HelpCommand() if not argument else InvalidCommand("/help 不接受参数")
    if command == "/resume":
        return ResumeCommand(argument or None)
    if command == "/mode":
        if argument in VALID_MODES - {"bypassPermissions"}:
            return ModeCommand(argument)
        return InvalidCommand("可用模式：default、acceptEdits、plan、dontAsk")
    return InvalidCommand("可用命令：/help、/new、/stop、/resume [session_id]、/mode <mode>")
