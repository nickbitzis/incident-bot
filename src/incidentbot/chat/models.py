"""Channels and messages. Message blocks use the Block Kit JSON shape directly, so the Slack
adapter passes them through unchanged."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Channel:
    id: str
    name: str
    topic: str = ""
    created_at: float = 0.0
    incident_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Message:
    id: str
    channel_id: str
    user: str
    text: str
    ts: float
    thread_ts: str | None = None  # id of the parent message, as in Slack threads
    blocks: list[dict[str, Any]] = field(default_factory=list)
    reactions: dict[str, list[str]] = field(default_factory=dict)  # name -> users
    bot: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def button(label: str, action_id: str, value: str, style: str | None = None) -> dict[str, Any]:
    b: dict[str, Any] = {"type": "button", "text": {"type": "plain_text", "text": label},
                         "action_id": action_id, "value": value}
    if style:
        b["style"] = style
    return b


def actions(*buttons: dict[str, Any]) -> dict[str, Any]:
    return {"type": "actions", "elements": list(buttons)}
