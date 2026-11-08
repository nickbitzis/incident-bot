"""The incident bot: turns chat events and bus events into a coordinated incident.

All state changes go through handle() (chat) or the bus subscription, so every change lands
on the incident timeline with a timestamp and an actor.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from incidentbot.chat import (
    ChatEvent,
    ChatStore,
    InMemoryChatStore,
    Interaction,
    Message,
    MessagePosted,
    ReactionAdded,
    SlashCommand,
    actions,
    button,
    context,
    section,
)
from incidentbot.clock import Clock, SystemClock
from incidentbot.commands import Command, CommandError, help_text, normalize_user, parse
from incidentbot.eventbus import BusEvent, EventBus
from incidentbot.logs import log
from incidentbot.models import (
    ROLE_ALIASES,
    ROLE_TITLES,
    SEVERITIES,
    STATES,
    TRANSITIONS,
    ActionItem,
    Incident,
    IncidentError,
)
from incidentbot.policy import policy_for, rank
from incidentbot.postmortem import PostmortemWriter, TemplateWriter, generate
from incidentbot.runbooks import RunbookError, RunbookExecutor, RunbookRun
from incidentbot.statuspage import ApprovalError, StatusIncident, StatusPage
from incidentbot.systems import SERVICES, FakeSystems

logger = logging.getLogger("incidentbot.bot")

BOT = "incidentbot"
STAR_REACTIONS = {"star", "pushpin"}
ACTION_TAG = re.compile(r"(^|\s)#action\b", re.I)
MENTION = re.compile(r"@([a-z0-9_.-]+)", re.I)
BACKFILL_SECONDS = 3600
STATE_LEVEL = {"monitoring": "degraded_performance", "resolved": "operational"}
NEEDS_INCIDENT_HINT = "Run this in an incident channel, or declare one: /incident declare ..."
UserError = (IncidentError, CommandError, RunbookError, ApprovalError)


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M")


def slug(text: str, limit: int = 32) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "incident"


class IncidentBot:
    def __init__(self, store: ChatStore | None = None, clock: Clock | None = None,
                 bus: EventBus | None = None, systems: FakeSystems | None = None,
                 executor: RunbookExecutor | None = None, status: StatusPage | None = None,
                 writer: PostmortemWriter | None = None, first_id: int = 1001) -> None:
        self.store = store or InMemoryChatStore()
        self.clock = clock or SystemClock()
        self.bus = bus or EventBus()
        self.systems = systems or FakeSystems.default()
        self.executor = executor or RunbookExecutor(self.systems)
        self.status = status or StatusPage()
        self.writer = writer or TemplateWriter()
        self.incidents: dict[str, Incident] = {}
        self.by_channel: dict[str, str] = {}
        self.counters: Counter[tuple[str, ...]] = Counter()
        self.lock = threading.RLock()
        self._next_id = first_id
        self._run_msgs: dict[str, str] = {}
        self._approval_msgs: dict[str, str] = {}
        self._recorded_steps: set[tuple[str, str]] = set()
        self._starred: set[str] = set()
        self._out: list[Message] = []
        now = self.clock.now()
        self.incidents_channel = self._channel("incidents", now, "Declare incidents here")
        self.alerts_channel = self._channel("ops-alerts", now, "Monitoring alerts and deploys")
        self.bus.subscribe(self._on_bus)

    def _channel(self, name: str, ts: float, topic: str):
        return self.store.channel_by_name(name) or self.store.create_channel(name, ts, topic)

    # helpers ----------------------------------------------------------------------
    def now(self) -> float:
        return self.clock.now()

    def say(self, channel_id: str, text: str, blocks: list[dict[str, Any]] | None = None,
            thread_ts: str | None = None) -> Message:
        m = self.store.post(channel_id, BOT, text, self.now(), thread_ts, blocks, bot=True)
        self._out.append(m)
        return m

    def incident_for(self, channel_id: str) -> Incident | None:
        iid = self.by_channel.get(channel_id)
        return self.incidents.get(iid) if iid else None

    def get(self, incident_id: str) -> Incident:
        inc = self.incidents.get(incident_id.upper())
        if inc is None:
            raise KeyError(incident_id)
        return inc

    def open_incidents(self) -> list[Incident]:
        return [i for i in self.incidents.values() if i.is_open]

    # entry point --------------------------------------------------------------------
    def handle(self, event: ChatEvent) -> list[Message]:
        with self.lock:
            self._out = []
            kind = type(event).__name__
            self.counters[("events", kind)] += 1
            if isinstance(event, SlashCommand):
                self._on_command(event)
            elif isinstance(event, MessagePosted):
                self._on_message(event)
            elif isinstance(event, ReactionAdded):
                self._on_reaction(event)
            elif isinstance(event, Interaction):
                self._on_interaction(event)
            else:  # pragma: no cover - type checked
                raise TypeError(kind)
            out, self._out = self._out, []
            return out

    def _on_command(self, ev: SlashCommand) -> None:
        self.store.post(ev.channel_id, ev.user, f"{ev.command} {ev.text}".strip(), self.now())
        if ev.command != "/incident":
            self.say(ev.channel_id, f"Unknown command {ev.command}. Try /incident help")
            return
        verb = "?"
        try:
            cmd = parse(ev.text)
            verb = cmd.verb
            inc = self.incident_for(ev.channel_id)
            if inc is None and verb not in ("declare", "help", "runbooks"):
                raise IncidentError(NEEDS_INCIDENT_HINT)
            getattr(self, f"_cmd_{verb}")(ev, cmd, inc)
            self.counters[("commands", verb, "ok")] += 1
            log(logger, "command", verb=verb, user=ev.user, channel=ev.channel_id,
                incident=inc.id if inc else None)
        except UserError as exc:
            self.counters[("commands", verb, "rejected")] += 1
            log(logger, "command rejected", verb=verb, user=ev.user, reason=str(exc))
            self.say(ev.channel_id, f"Not done: {exc}")

    # commands -----------------------------------------------------------------------
    def _cmd_help(self, ev, cmd, inc):
        self.say(ev.channel_id, "Incident commands:\n" + help_text())

    def _cmd_declare(self, ev: SlashCommand, cmd: Command, inc: Incident | None) -> Incident:
        if inc is not None:
            raise IncidentError(f"this channel already belongs to {inc.id}")
        sev = cmd.arg(0, "severity").lower()
        if sev not in SEVERITIES:
            raise IncidentError(f"severity must be one of {', '.join(SEVERITIES)}")
        title = " ".join(cmd.args[1:]).strip()
        if not title:
            raise IncidentError('add a title in quotes: /incident declare sev2 "checkout errors"')
        service = cmd.options.get("service") or next(
            (s for s in SERVICES if s in title.lower()), "")
        if service not in SERVICES:
            raise IncidentError(f"add service=<name>. Known services: {', '.join(SERVICES)}")
        commander = normalize_user(cmd.options.get("ic", ev.user))
        return self.declare(sev, title, service, ev.user, commander, cmd.raw)

    def declare(self, sev: str, title: str, service: str, actor: str, commander: str,
                raw: str = "") -> Incident:
        now = self.now()
        iid = f"INC-{self._next_id}"
        self._next_id += 1
        ch = self.store.create_channel(f"inc-{iid[4:]}-{slug(title)}", now,
                                       f"{sev} {title} ({service})", iid)
        recent = self.bus.recent(service, now - BACKFILL_SECONDS, now)
        alerts = [e for e in recent if e.kind == "alert"]
        detected = min([e.ts for e in alerts] + [now])
        inc = Incident(iid, title, sev, service, ch.id, actor, now, detected, last_update_at=now)
        self.incidents[iid] = inc
        self.by_channel[ch.id] = iid
        for e in recent:
            self._bus_entry(inc, e)
        inc.add(now, "declare", actor, f"{sev} declared by @{actor}: {title}",
                command=raw, service=service)
        pol = policy_for(sev)
        cadence = f"updates every {pol.cadence_minutes} minutes" if pol.cadence_minutes else (
            "no update cadence")
        self.say(self.incidents_channel.id,
                 f"{iid} {sev} declared by @{actor}: {title}. Join #{ch.name}",
                 [section(f"*{iid}* {sev}: {title}"),
                  context(f"service {service}, IC @{commander}, channel #{ch.name}")])
        self.say(ch.id, f"{iid} {sev}: {title}", [
            section(f"*{iid}* {sev}: {title}\nService *{service}*. Incident commander "
                    f"@{commander}. Policy: {pol.description.lower()}, {cadence}."),
            actions(button("I'll take comms", "claim_role", "comms"),
                    button("I'll take ops", "claim_role", "ops")),
            context("Star a message with :star: to put it on the timeline. Tag follow ups "
                    "with #action. /incident help lists every command."),
        ])
        self._assign(inc, "commander", commander, actor, None)
        self._apply_policy(inc)
        log(logger, "incident declared", incident=iid, severity=sev, service=service)
        return inc

    def _apply_policy(self, inc: Incident) -> None:
        pol = policy_for(inc.severity)
        now = self.now()
        paged = {p["target"] for p in inc.pages}
        targets = [t for t in pol.page if t not in paged]
        if targets:
            for t in targets:
                inc.pages.append({"target": t, "ts": now, "severity": inc.severity})
            inc.add(now, "page", BOT, f"paged {', '.join(targets)}", "bot", targets=targets)
            self.say(inc.channel_id, f"Paged {', '.join(targets)} ({inc.severity} policy).")
        self.status.set_internal(inc.service, pol.public_status)
        if pol.status_page:
            si = self._status_incident(inc)
            if si.public_state is None and not self.status.pending(inc.id):
                inc.add(now, "status", BOT, f"opened status page incident {si.id} for "
                        f"{si.component}", "bot")
                self._draft_public(inc, inc.state, None, BOT)

    def _assign(self, inc: Incident, role: str, user: str, actor: str,
                previous: str | None) -> None:
        inc.roles[role] = user
        title = ROLE_TITLES[role]
        if previous:
            text = f"{title} handed off from @{previous} to @{user}"
        else:
            text = f"@{user} is the {title.lower()}"
        inc.add(self.now(), "role", actor, text, role=role, user=user, previous=previous)
        self.say(inc.channel_id, text + ".")

    def _role(self, token: str) -> str:
        role = ROLE_ALIASES.get(token.lower())
        if role is None:
            raise IncidentError(f"unknown role {token!r}. Use commander, comms, or ops")
        return role

    def _cmd_role(self, ev, cmd, inc):
        role = self._role(cmd.arg(0, "role"))
        user = normalize_user(cmd.args[1]) if len(cmd.args) > 1 else ev.user
        self.assign_role(inc, role, user, ev.user)

    def assign_role(self, inc: Incident, role: str, user: str, actor: str) -> None:
        current = inc.roles.get(role)
        if current == user:
            raise IncidentError(f"@{user} already is the {ROLE_TITLES[role].lower()}")
        if current:
            raise IncidentError(f"@{current} is the {ROLE_TITLES[role].lower()}. "
                                f"Use /incident handoff {role} @user")
        self._assign(inc, role, user, actor, None)

    def _cmd_handoff(self, ev, cmd, inc):
        role = self._role(cmd.arg(0, "role"))
        user = normalize_user(cmd.arg(1, "user"))
        current = inc.roles.get(role)
        if current is None:
            raise IncidentError(f"nobody holds {role} yet. Use /incident role {role} @{user}")
        if ev.user not in (current, inc.roles["commander"]):
            raise IncidentError(f"only @{current} or the incident commander can hand off {role}")
        if user == current:
            raise IncidentError(f"@{user} already is the {ROLE_TITLES[role].lower()}")
        self._assign(inc, role, user, ev.user, current)

    def _cmd_sev(self, ev, cmd, inc):
        new = cmd.arg(0, "severity").lower()
        if new not in SEVERITIES:
            raise IncidentError(f"severity must be one of {', '.join(SEVERITIES)}")
        if not inc.is_open:
            raise IncidentError(f"{inc.id} is resolved")
        if new == inc.severity:
            raise IncidentError(f"{inc.id} already is {new}")
        old = inc.severity
        inc.severity = new
        reason = " ".join(cmd.args[1:])
        verb = "escalated" if rank(new) < rank(old) else "downgraded"
        text = f"severity {verb} from {old} to {new} by @{ev.user}" + (
            f": {reason}" if reason else "")
        inc.add(self.now(), "severity", ev.user, text, old=old, new=new)
        self.say(inc.channel_id, text[0].upper() + text[1:] + ".")
        if rank(new) < rank(old):
            self._apply_policy(inc)
        else:
            self.status.set_internal(inc.service, policy_for(new).public_status)

    def _cmd_state(self, ev, cmd, inc):
        self.transition(inc, cmd.arg(0, "state").lower(), ev.user)

    def _cmd_resolve(self, ev, cmd, inc):
        self.transition(inc, "resolved", ev.user)

    def transition(self, inc: Incident, new: str, actor: str) -> None:
        if new not in STATES:
            raise IncidentError(f"state must be one of {', '.join(STATES)}")
        if new == inc.state:
            raise IncidentError(f"{inc.id} already is {new}")
        if new not in TRANSITIONS[inc.state]:
            hint = " Mark it identified or monitoring first." if new == "resolved" else ""
            raise IncidentError(f"cannot go from {inc.state} to {new}.{hint}")
        now = self.now()
        old, inc.state = inc.state, new
        if new == "identified" and inc.identified_at is None:
            inc.identified_at = now
        if new == "monitoring" and inc.mitigated_at is None:
            inc.mitigated_at = now
        if new == "resolved":
            inc.resolved_at = now
            inc.mitigated_at = inc.mitigated_at or now
        inc.add(now, "state", actor, f"state {old} -> {new} by @{actor}", old=old, new=new)
        self.say(inc.channel_id, f"{inc.id} is now *{new}* (was {old}).")
        self.status.set_internal(inc.service, STATE_LEVEL.get(new, policy_for(
            inc.severity).public_status))
        si = self._status_incident(inc) if inc.status_incident_id else None
        if si is not None and si.public_state is not None and new == "resolved":
            self._draft_public(inc, "resolved", None, BOT)
        if new == "resolved":
            inc.postmortem = generate(inc, self.writer, now)
            open_items = sum(not a.done for a in inc.action_items)
            self.say(inc.channel_id,
                     f"Postmortem draft ready for {inc.id} ({inc.postmortem['generator']}). "
                     f"{open_items} open action item(s).",
                     [section(f"Postmortem *draft* ready for {inc.id}, written by "
                              f"{inc.postmortem['generator']}. Edit it before sharing."),
                      context(f"{len(inc.timeline)} timeline entries, {open_items} open "
                              f"action items. Open it at /#/incident/{inc.id}")])
            log(logger, "incident resolved", incident=inc.id,
                minutes=round((now - inc.detected_at) / 60))

    def _cmd_update(self, ev, cmd, inc):
        text = " ".join(cmd.args).strip()
        if not text:
            raise IncidentError('add the update text: /incident update "what we know"')
        now = self.now()
        self._count_update(inc, now)
        inc.add(now, "update", ev.user, f"update from @{ev.user}: {text}")
        si = self._status_incident(inc)
        self.status.post_internal(si, inc.state, text, ev.user, now)
        self.say(inc.channel_id, f"Update from @{ev.user}: {text}",
                 [section(f"*Status update* ({inc.state}) from @{ev.user}\n>{text}")])

    def _count_update(self, inc: Incident, ts: float) -> None:
        inc.updates.append(ts)
        inc.last_update_at = ts

    def _status_incident(self, inc: Incident) -> StatusIncident:
        if inc.status_incident_id:
            return self.status.incidents[inc.status_incident_id]
        si = self.status.open(inc.id, inc.title, inc.service,
                              policy_for(inc.severity).public_status, self.now())
        inc.status_incident_id = si.id
        return si

    def _cmd_status(self, ev, cmd, inc):
        self._draft_public(inc, inc.state, " ".join(cmd.args).strip() or None, ev.user)

    def _draft_public(self, inc: Incident, state: str, body: str | None, author: str) -> None:
        si = self._status_incident(inc)
        pol = policy_for(inc.severity)
        body = body or self.status.render(state, inc.title, si.component, pol.cadence_minutes)
        u = self.status.draft_public(si, state, body, author, self.now())
        inc.add(self.now(), "status", author,
                f"public status update {u.id} drafted ({state}), waiting for comms approval",
                "bot" if author == BOT else "chat", update_id=u.id)
        comms = inc.roles.get("comms")
        who = f"@{comms}" if comms else "the comms lead (assign one with /incident role comms)"
        m = self.say(inc.channel_id, f"Public status draft {u.id}: {body}", [
            section(f"*Public status draft {u.id}* for {si.component}\n>{body}"),
            context(f"Needs approval from {who}."),
            actions(button("Approve and publish", "status_approve", u.id, "primary"),
                    button("Reject", "status_reject", u.id, "danger")),
        ])
        self._approval_msgs[u.id] = m.id

    def _cmd_approve(self, ev, cmd, inc):
        self.approve_status(inc, cmd.arg(0, "update id"), ev.user)

    def approve_status(self, inc: Incident, update_id: str, actor: str) -> None:
        now = self.now()
        u = self.status.approve(update_id, actor, inc.roles.get("comms"), now)
        self._count_update(inc, now)
        inc.add(now, "status", actor, f"public status update {u.id} approved by @{actor} and "
                f"published ({u.state})", update_id=u.id)
        self._close_buttons(self._approval_msgs.get(u.id), f"Published by @{actor} at "
                            f"{hhmm(now)} UTC.")
        self.say(inc.channel_id, f"Published {u.id} to the public status page.")

    def _cmd_reject(self, ev, cmd, inc):
        self.reject_status(inc, cmd.arg(0, "update id"), ev.user, " ".join(cmd.args[1:]))

    def reject_status(self, inc: Incident, update_id: str, actor: str, reason: str) -> None:
        now = self.now()
        u = self.status.reject(update_id, actor, inc.roles.get("comms"), reason, now)
        inc.add(now, "status", actor, f"public status update {u.id} rejected by @{actor}"
                + (f": {reason}" if reason else ""), update_id=u.id)
        self._close_buttons(self._approval_msgs.get(u.id), f"Rejected by @{actor}.")
        self.say(inc.channel_id, f"Rejected {u.id}. Draft a new one with /incident status.")

    def _close_buttons(self, message_id: str | None, note: str) -> None:
        if not message_id:
            return
        m = self.store.get_message(message_id)
        if m is None:
            return
        blocks = [b for b in m.blocks if b.get("type") != "actions"]
        self.store.update_blocks(message_id, blocks + [context(note)])

    # runbooks -----------------------------------------------------------------------
    def _cmd_runbooks(self, ev, cmd, inc):
        lines = [f"`{rb.name}`: {rb.title}. Params: "
                 + ", ".join(f"{k}{'' if v.get('required') else '?'}" for k, v in rb.params.items())
                 for rb in self.executor.runbooks.values()]
        self.say(ev.channel_id, "Runbooks:\n" + "\n".join(lines))

    def _cmd_runbook(self, ev, cmd, inc):
        name = cmd.arg(0, "runbook name")
        rb = self.executor.runbooks.get(name)
        params = dict(cmd.options)
        if rb is not None and "service" in rb.params and not params.get("service"):
            params["service"] = inc.service
        self.run_runbook(inc, name, params, ev.user, dry_run="dry-run" in cmd.flags,
                         force="force" in cmd.flags)

    def run_runbook(self, inc: Incident, name: str, params: dict[str, str], actor: str,
                    dry_run: bool = False, force: bool = False) -> RunbookRun:
        if not inc.is_open and not dry_run:
            raise IncidentError(f"{inc.id} is resolved. Use --dry-run to preview")
        now = self.now()
        run, reused = self.executor.start(name, params, inc.id, actor, now, dry_run, force)
        if reused:
            inc.add(now, "runbook", actor, f"runbook {name} not started again: same parameters "
                    f"as {run.id} ({run.status})", run_id=run.id)
            self.say(inc.channel_id, f"{run.id} already ran {name} with these parameters "
                     f"({run.status}). Nothing was repeated. Add --force to run it again.")
            return run
        inc.runbook_runs.append(run.id)
        label = " (dry run)" if dry_run else ""
        inc.add(now, "runbook", actor, f"runbook {name} {run.id} started by @{actor}{label}",
                run_id=run.id, params=run.params, dry_run=dry_run)
        self.counters[("runbook_runs", name, "dry_run" if dry_run else "live")] += 1
        self._record_steps(inc, run)
        m = self.say(inc.channel_id, f"Runbook {name} {run.id}: {run.status}",
                     self._run_blocks(run, inc))
        self._run_msgs[run.id] = m.id
        return run

    def _run_blocks(self, run: RunbookRun, inc: Incident) -> list[dict[str, Any]]:
        mark = {"done": "[done]", "planned": "[plan]", "awaiting_confirmation": "[waiting]",
                "pending": "[ ]", "failed": "[failed]", "cancelled": "[cancelled]"}
        lines = [f"{mark[s.status]} {s.description}: {s.result or s.summary or s.action}"
                 for s in run.steps]
        status = "dry run, nothing changed" if run.dry_run else run.status.replace("_", " ")
        title = f"*Runbook {run.runbook}* {run.id}: {status}"
        blocks = [section(title + "\n" + "\n".join(lines))]
        step = run.current()
        if run.status == "awaiting_confirmation" and step:
            who = [f"@{u}" for u in (inc.roles.get("ops"), inc.roles.get("commander")) if u]
            blocks += [context(f"Confirm: {step.summary}. Allowed: {' or '.join(who)}."),
                       actions(button("Confirm", "runbook_confirm", run.id, "primary"),
                               button("Cancel", "runbook_cancel", run.id, "danger"))]
        return blocks

    def _record_steps(self, inc: Incident, run: RunbookRun) -> None:
        for s in run.steps:
            key = (run.id, s.id)
            if key in self._recorded_steps or s.status not in ("done", "failed", "planned"):
                continue
            self._recorded_steps.add(key)
            self.counters[("runbook_steps", s.action, s.status)] += 1
            if s.status == "planned":
                text = f"{run.id} {s.id}: {s.result}"
            elif s.status == "failed":
                text = f"{run.id} {s.id} failed: {s.result}"
            else:
                verb = "applied" if s.changed else "checked"
                text = f"{run.id} {s.id} {verb}: {s.result}"
            inc.add(s.ts or self.now(), "runbook", s.by or run.actor, text, run_id=run.id,
                    step=s.id, action=s.action, changed=s.changed)

    def confirm_run(self, inc: Incident, run_id: str, actor: str) -> RunbookRun:
        allowed = {inc.roles.get("ops"), inc.roles.get("commander")} - {None}
        if actor not in allowed:
            raise IncidentError("only the ops lead or the incident commander can confirm "
                                "runbook steps")
        run, acted = self.executor.confirm(run_id, actor, self.now())
        if not acted:
            self.say(inc.channel_id, f"{run.id} has nothing waiting for confirmation "
                     f"({run.status}). No action taken.")
            return run
        self._record_steps(inc, run)
        self._refresh_run(inc, run)
        self.say(inc.channel_id, f"@{actor} confirmed {run.id}: {run.status}.")
        return run

    def cancel_run(self, inc: Incident, run_id: str, actor: str) -> RunbookRun:
        run, acted = self.executor.cancel(run_id, actor, self.now())
        if acted:
            inc.add(self.now(), "runbook", actor, f"{run.id} cancelled by @{actor}",
                    run_id=run.id)
            self._refresh_run(inc, run)
        return run

    def _refresh_run(self, inc: Incident, run: RunbookRun) -> None:
        mid = self._run_msgs.get(run.id)
        if mid:
            self.store.update_blocks(mid, self._run_blocks(run, inc))

    # action items and notes -----------------------------------------------------------
    def _cmd_action(self, ev, cmd, inc):
        text = " ".join(cmd.args).strip()
        if not text:
            raise IncidentError('add the action item text: /incident action "add an alert"')
        owner = normalize_user(cmd.options["owner"]) if cmd.options.get("owner") else None
        e = inc.add(self.now(), "action", ev.user, f"action item: {text}", owner=owner)
        self._add_action(inc, text, owner, e.id)

    def _add_action(self, inc: Incident, text: str, owner: str | None, entry_id: str) -> None:
        item = ActionItem(f"A{len(inc.action_items) + 1}", text, owner, self.now(), entry_id)
        inc.action_items.append(item)
        who = f" for @{owner}" if owner else ""
        self.say(inc.channel_id, f"Action item {item.id}{who}: {text}")

    def _cmd_done(self, ev, cmd, inc):
        aid = cmd.arg(0, "action item id").upper()
        item = next((a for a in inc.action_items if a.id == aid), None)
        if item is None:
            raise IncidentError(f"no action item {aid} on {inc.id}")
        if item.done:
            raise IncidentError(f"{aid} is already done")
        item.done, item.done_at = True, self.now()
        inc.add(self.now(), "action", ev.user, f"action item {aid} done by @{ev.user}",
                item=aid)
        self.say(inc.channel_id, f"{aid} marked done.")

    def _cmd_note(self, ev, cmd, inc):
        text = " ".join(cmd.args).strip()
        if not text:
            raise IncidentError("add the note text")
        inc.add(self.now(), "note", ev.user, f"note from @{ev.user}: {text}")
        self.say(inc.channel_id, "Noted on the timeline.")

    def _cmd_timeline(self, ev, cmd, inc):
        inc.add(self.now(), "command", ev.user, "timeline viewed")
        rows = [f"{hhmm(e.ts)} [{e.id}] {e.text}" for e in inc.sorted_timeline()
                if e.kind != "command"][-12:]
        self.say(inc.channel_id, "Latest timeline entries (UTC):\n" + "\n".join(rows))

    # messages, reactions, buttons -----------------------------------------------------
    def _on_message(self, ev: MessagePosted) -> None:
        m = self.store.post(ev.channel_id, ev.user, ev.text, self.now(), ev.thread_ts)
        inc = self.incident_for(ev.channel_id)
        if inc is None or not ACTION_TAG.search(ev.text):
            return
        text = ACTION_TAG.sub(" ", ev.text).strip()
        mention = MENTION.search(text)
        owner = mention.group(1).lower() if mention else None
        text = re.sub(r"\s+", " ", MENTION.sub("", text)).strip()
        e = inc.add(self.now(), "action", ev.user, f"action item from @{ev.user}: {text}",
                    message_id=m.id, owner=owner)
        self._add_action(inc, text, owner, e.id)

    def _on_reaction(self, ev: ReactionAdded) -> None:
        msg = self.store.get_message(ev.message_id)
        if msg is None:
            return
        self.store.add_reaction(ev.message_id, ev.reaction, ev.user)
        inc = self.incident_for(msg.channel_id)
        if inc is None or ev.reaction not in STAR_REACTIONS or msg.id in self._starred:
            return
        self._starred.add(msg.id)
        e = inc.add(self.now(), "message", msg.user, f"@{msg.user}: {msg.text}",
                    message_id=msg.id, starred_by=ev.user, said_at=msg.ts)
        e.ts = msg.ts  # the timeline shows when it was said, not when it was starred
        self.say(inc.channel_id, f"Added to the timeline ({e.id}).", thread_ts=msg.id)

    def _on_interaction(self, ev: Interaction) -> None:
        inc = self.incident_for(ev.channel_id)
        try:
            if ev.action_id == "declare_from_alert":
                sev, service, title = ev.value.split("|", 2)
                self.declare(sev, title, service, ev.user, ev.user, f"button {ev.value}")
                return
            if inc is None:
                raise IncidentError(NEEDS_INCIDENT_HINT)
            if ev.action_id == "claim_role":
                self.assign_role(inc, self._role(ev.value), ev.user, ev.user)
            elif ev.action_id == "runbook_confirm":
                self.confirm_run(inc, ev.value, ev.user)
            elif ev.action_id == "runbook_cancel":
                self.cancel_run(inc, ev.value, ev.user)
            elif ev.action_id == "status_approve":
                self.approve_status(inc, ev.value, ev.user)
            elif ev.action_id == "status_reject":
                self.reject_status(inc, ev.value, ev.user, "")
            else:
                raise IncidentError(f"unknown action {ev.action_id}")
        except UserError as exc:
            self.say(ev.channel_id, f"Not done: {exc}")

    # bus ------------------------------------------------------------------------------
    def _bus_entry(self, inc: Incident, e: BusEvent) -> None:
        inc.add(e.ts, e.kind, e.data.get("author", "monitoring"), e.summary, "bus",
                event_id=e.id, **{k: v for k, v in e.data.items() if k != "author"})
        if e.kind == "alert":
            inc.affected_users = max(inc.affected_users, int(e.data.get("affected_users", 0)))

    def _on_bus(self, e: BusEvent) -> None:
        with self.lock:
            if e.kind == "deploy":
                self.systems.record_deploy(e.service, e.data["version"])
            elif e.kind == "flag_change" and e.data.get("flag") in self.systems.flags:
                self.systems.flags[e.data["flag"]] = bool(e.data["enabled"])
            elif e.kind == "alert" and e.data.get("metric") == "error_rate":
                self.systems.error_rate[e.service] = float(e.data["value"])
            self.counters[("bus_events", e.kind)] += 1
            affected = [i for i in self.open_incidents() if i.service == e.service]
            for inc in affected:
                self._bus_entry(inc, e)
                self.say(inc.channel_id, e.summary, [context(f"{e.kind}: {e.summary}")])
            if e.kind == "alert" and not affected:
                title = f"{e.data.get('metric', 'alert').replace('_', ' ')} on {e.service}"
                self.say(self.alerts_channel.id, e.summary, [
                    section(f"*Alert* {e.summary}"),
                    actions(button("Declare sev2", "declare_from_alert",
                                   f"sev2|{e.service}|{title}", "primary"),
                            button("Declare sev1", "declare_from_alert",
                                   f"sev1|{e.service}|{title}", "danger")),
                ])
            elif not affected:
                self.say(self.alerts_channel.id, e.summary, [context(e.summary)])

    # cadence ---------------------------------------------------------------------------
    def tick(self) -> list[Message]:
        """Post update reminders for incidents that missed their cadence. Call every minute."""
        with self.lock:
            self._out = []
            now = self.now()
            for inc in self.open_incidents():
                cadence = policy_for(inc.severity).cadence_minutes
                if cadence is None:
                    continue
                last_ping = max([inc.last_update_at] + inc.reminders)
                if now < last_ping + cadence * 60:
                    continue
                silent = round((now - inc.last_update_at) / 60)
                who = inc.roles.get("comms") or inc.roles.get("commander")
                inc.reminders.append(now)
                inc.add(now, "reminder", BOT, f"update reminder: {silent} minutes without an "
                        f"update (policy {cadence})", "bot")
                self.counters[("reminders", inc.severity)] += 1
                self.say(inc.channel_id,
                         f"@{who} {silent} minutes since the last update on this "
                         f"{inc.severity}. Policy is every {cadence} minutes. Post one with "
                         f"/incident update \"...\"")
            out, self._out = self._out, []
            return out
