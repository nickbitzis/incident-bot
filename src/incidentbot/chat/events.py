"""Inbound chat events. The bot only sees these four types, whatever the transport."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommand:
    user: str
    channel_id: str
    command: str  # "/incident"
    text: str  # everything after the command


@dataclass(frozen=True)
class MessagePosted:
    user: str
    channel_id: str
    text: str
    thread_ts: str | None = None


@dataclass(frozen=True)
class ReactionAdded:
    user: str
    channel_id: str
    message_id: str
    reaction: str


@dataclass(frozen=True)
class Interaction:
    """A button press. message_id is the message that carried the button."""

    user: str
    channel_id: str
    message_id: str
    action_id: str
    value: str


ChatEvent = SlashCommand | MessagePosted | ReactionAdded | Interaction
