"""Parse `/incident ...` slash command text into a Command.

Grammar: /incident <verb> [positional ...] [key=value ...] [--flag ...]
Quotes group words: /incident declare sev2 "checkout errors" service=checkout
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

VERBS = {
    "declare": "declare <sev1-4> \"title\" [service=name] [ic=@user]",
    "role": "role <commander|comms|ops> @user",
    "handoff": "handoff <commander|comms|ops> @user",
    "sev": "sev <sev1-4> [reason]",
    "state": "state <investigating|identified|monitoring|resolved>",
    "resolve": "resolve",
    "update": "update \"internal status update\"",
    "status": "status [\"public text\"]  (drafts a public update for the comms lead)",
    "approve": "approve <update id>",
    "reject": "reject <update id> [reason]",
    "runbook": "runbook <name> [key=value ...] [--dry-run] [--force]",
    "runbooks": "runbooks",
    "action": "action \"text\" [owner=@user]",
    "done": "done <action item id>",
    "note": "note \"text\"",
    "timeline": "timeline",
    "help": "help",
}
ALIASES = {"severity": "sev", "ack": "role", "page": "declare", "item": "action",
           "rb": "runbook", "postmortem": "timeline"}


class CommandError(ValueError):
    pass


@dataclass
class Command:
    verb: str
    args: list[str] = field(default_factory=list)
    options: dict[str, str] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)
    raw: str = ""

    def arg(self, i: int, name: str) -> str:
        if len(self.args) <= i:
            raise CommandError(f"missing {name}. Usage: /incident {VERBS[self.verb]}")
        return self.args[i]


def normalize_user(token: str) -> str:
    """'@ana' and '<@ana>' (Slack mention format) both become 'ana'."""
    t = token.strip()
    if t.startswith("<@") and t.endswith(">"):
        t = t[2:-1].split("|")[0]
    return t.lstrip("@").lower()


def parse(text: str) -> Command:
    raw = text.strip()
    if raw.startswith("/incident"):
        raw = raw[len("/incident"):].strip()
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        raise CommandError(f"could not parse command: {exc}") from exc
    if not tokens:
        return Command("help", raw=raw)
    verb = tokens[0].lower()
    verb = ALIASES.get(verb, verb)
    if verb not in VERBS:
        raise CommandError(f"unknown command {tokens[0]!r}. Try /incident help")
    cmd = Command(verb, raw=raw)
    for tok in tokens[1:]:
        if tok.startswith("--") and len(tok) > 2:
            cmd.flags.add(tok[2:].lower())
        elif "=" in tok and not tok.startswith("=") and " " not in tok.split("=", 1)[0]:
            k, v = tok.split("=", 1)
            cmd.options[k.lower()] = v
        else:
            cmd.args.append(tok)
    return cmd


def help_text() -> str:
    return "\n".join(f"`/incident {usage}`" for usage in VERBS.values())
