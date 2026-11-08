import json

import pytest

from incidentbot.chat import Interaction, Message, MessagePosted, ReactionAdded, SlashCommand
from incidentbot.chat.slack import UnsupportedPayload, from_slack, parse_form, to_chat_post_message


def test_slash_command_form_body():
    body = b"command=%2Fincident&text=declare+sev2+%22checkout%22&user_id=U1&channel_id=C1"
    [ev] = from_slack(parse_form(body))
    assert ev == SlashCommand("U1", "C1", "/incident", 'declare sev2 "checkout"')


def test_message_event_with_thread():
    payload = {"type": "event_callback", "event": {"type": "message", "user": "U1",
               "channel": "C1", "text": "hi", "thread_ts": "M1"}}
    assert from_slack(payload) == [MessagePosted("U1", "C1", "hi", "M1")]


def test_bot_and_edited_messages_are_ignored():
    for extra in ({"bot_id": "B1"}, {"subtype": "message_changed"}):
        ev = {"type": "message", "user": "U1", "channel": "C1", "text": "x", **extra}
        assert from_slack({"type": "event_callback", "event": ev}) == []


def test_reaction_added():
    payload = {"type": "event_callback", "event": {
        "type": "reaction_added", "user": "U2", "reaction": "star",
        "item": {"type": "message", "channel": "C1", "ts": "M9"}}}
    assert from_slack(payload) == [ReactionAdded("U2", "C1", "M9", "star")]


def test_block_actions_from_form_payload():
    inner = {"type": "block_actions", "user": {"id": "U3"}, "channel": {"id": "C1"},
             "message": {"ts": "M5"},
             "actions": [{"action_id": "runbook_confirm", "value": "RB-001"}]}
    [ev] = from_slack({"payload": json.dumps(inner)})
    assert ev == Interaction("U3", "C1", "M5", "runbook_confirm", "RB-001")


def test_unknown_payload_rejected():
    with pytest.raises(UnsupportedPayload):
        from_slack({"type": "view_submission"})


def test_outbound_blocks_pass_through():
    m = Message("M1", "C1", "incidentbot", "t", 1.0, "M0", [{"type": "section"}])
    assert to_chat_post_message(m, "C0SLACK") == {
        "channel": "C0SLACK", "text": "t", "blocks": [{"type": "section"}], "thread_ts": "M0"}
