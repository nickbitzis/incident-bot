"""Adapter boundary to Slack. No Slack SDK: these functions map payloads to and from the four
internal event types and Block Kit messages. A real deployment would add request signature
verification (X-Slack-Signature, HMAC SHA256 over v0:timestamp:body) in front of this.

Mapping:
- Slash command (application/x-www-form-urlencoded: command, text, user_id, channel_id)
  -> SlashCommand
- Events API `event_callback` with event.type "message" (no subtype) -> MessagePosted
  (event.thread_ts -> thread_ts)
- Events API `event_callback` with event.type "reaction_added" (item.ts) -> ReactionAdded
- Interactivity `block_actions` (payload form field holding JSON) -> Interaction, one per action
- Outbound Message.blocks are already Block Kit (section, context, actions with buttons), so
  to_chat_post_message() only renames fields for chat.postMessage.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

from incidentbot.chat.events import (
    ChatEvent,
    Interaction,
    MessagePosted,
    ReactionAdded,
    SlashCommand,
)
from incidentbot.chat.models import Message


class UnsupportedPayload(ValueError):
    pass


def parse_form(body: bytes) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8"), keep_blank_values=True).items()}


def from_slack(payload: dict[str, Any]) -> list[ChatEvent]:
    """Map one decoded Slack payload to internal events. url_verification is handled by the
    caller because it needs a direct response."""
    if "command" in payload:
        return [SlashCommand(payload["user_id"], payload["channel_id"], payload["command"],
                             payload.get("text", ""))]
    if "payload" in payload:  # interactivity posts a form field with JSON inside
        payload = json.loads(payload["payload"])
    kind = payload.get("type")
    if kind == "block_actions":
        user = payload["user"]["id"]
        channel = payload["channel"]["id"]
        message_id = payload.get("message", {}).get("ts", "")
        return [Interaction(user, channel, message_id, a["action_id"], a.get("value", ""))
                for a in payload.get("actions", [])]
    if kind == "event_callback":
        ev = payload["event"]
        if ev.get("type") == "message" and not ev.get("subtype") and not ev.get("bot_id"):
            return [MessagePosted(ev["user"], ev["channel"], ev.get("text", ""),
                                  ev.get("thread_ts"))]
        if ev.get("type") == "reaction_added" and ev.get("item", {}).get("type") == "message":
            return [ReactionAdded(ev["user"], ev["item"]["channel"], ev["item"]["ts"],
                                  ev["reaction"])]
        return []
    raise UnsupportedPayload(f"unsupported Slack payload type {kind!r}")


def to_chat_post_message(msg: Message, channel_slack_id: str | None = None) -> dict[str, Any]:
    """Body for Slack's chat.postMessage. Internal message ids stand in for Slack ts values."""
    body: dict[str, Any] = {"channel": channel_slack_id or msg.channel_id, "text": msg.text}
    if msg.blocks:
        body["blocks"] = msg.blocks
    if msg.thread_ts:
        body["thread_ts"] = msg.thread_ts
    return body
