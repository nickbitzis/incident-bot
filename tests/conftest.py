from __future__ import annotations

import pytest

from incidentbot.bot import IncidentBot
from incidentbot.chat import Interaction, MessagePosted, ReactionAdded, SlashCommand
from incidentbot.clock import FakeClock


class Harness:
    """A bot on a fake clock plus shortcuts for sending chat events."""

    def __init__(self) -> None:
        self.clock = FakeClock(1_783_000_000)
        self.bot = IncidentBot(clock=self.clock)

    def cmd(self, user: str, text: str, channel: str | None = None):
        ch = channel or self.channel
        return self.bot.handle(SlashCommand(user, ch, "/incident", text))

    def msg(self, user: str, text: str, channel: str | None = None):
        self.bot.handle(MessagePosted(user, channel or self.channel, text))
        return self.bot.store.messages(channel or self.channel, 50)

    def react(self, user: str, message_id: str, reaction: str = "star"):
        return self.bot.handle(ReactionAdded(user, self.channel, message_id, reaction))

    def press(self, user: str, action_id: str, value: str, channel: str | None = None):
        return self.bot.handle(Interaction(user, channel or self.channel, "", action_id, value))

    def declare(self, sev: str = "sev2", title: str = "checkout errors",
                service: str = "checkout", user: str = "hiro"):
        self.bot.handle(SlashCommand(user, self.bot.incidents_channel.id, "/incident",
                                     f'declare {sev} "{title}" service={service}'))
        self.inc = max(self.bot.incidents.values(), key=lambda i: i.declared_at)
        self.channel = self.inc.channel_id
        return self.inc

    def last_text(self) -> str:
        return self.bot.store.messages(self.channel, 1)[-1].text

    def kinds(self) -> list[str]:
        return [e.kind for e in self.inc.sorted_timeline()]


@pytest.fixture
def h() -> Harness:
    return Harness()


@pytest.fixture
def staffed(h: Harness) -> Harness:
    """A sev2 on checkout with all three roles filled: hiro IC, grace comms, kofi ops."""
    h.declare()
    h.cmd("hiro", "role comms @grace")
    h.cmd("hiro", "role ops @kofi")
    return h
