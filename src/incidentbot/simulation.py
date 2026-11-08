"""Scripted simulation: 20 incidents over one quarter (July to September 2026) at Tillwave, a
made-up invoicing and payments SaaS.

Every incident is driven through the real bot: bus events for deploys and alerts, slash
commands, chat messages, star reactions, and button presses, with a fake clock that ticks
minute by minute while incidents are open so cadence reminders fire as they would live.
Chat lines come from templates per incident kind with seeded randomness.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from incidentbot.bot import IncidentBot
from incidentbot.chat import Interaction, MessagePosted, ReactionAdded, SlashCommand
from incidentbot.clock import FakeClock
from incidentbot.eventbus import alert, alert_resolved, deploy, flag_change
from incidentbot.models import Incident
from incidentbot.policy import policy_for
from incidentbot.postmortem import validate_narrative

QUARTER_START = datetime(2026, 7, 1, tzinfo=UTC).timestamp()
QUARTER_END = datetime(2026, 9, 30, 23, 59, tzinfo=UTC).timestamp()
USERS = ("ana", "ben", "chioma", "dmitri", "elif", "farid", "grace", "hiro", "ines", "jonah",
         "kofi", "lena")

# kind -> list of (service, title, extra). extra is a flag name for flag regressions.
SCENARIOS: dict[str, list[tuple[str, str, str]]] = {
    "bad_deploy": [("checkout", "checkout errors", ""),
                   ("payments-api", "card payments failing", ""),
                   ("invoices-api", "invoice creation returning 500s", ""),
                   ("web-app", "dashboard fails to load", ""),
                   ("auth", "SSO logins failing", ""),
                   ("webhooks", "webhook signatures rejected", "")],
    "traffic_spike": [("search", "search latency over 3 seconds", ""),
                      ("webhooks", "webhook deliveries delayed", ""),
                      ("reporting", "report exports timing out", ""),
                      ("invoices-api", "invoice API slow for large tenants", "")],
    "stale_cache": [("auth", "users signed out after login", ""),
                    ("invoices-api", "stale invoice totals shown", ""),
                    ("web-app", "old pricing shown on the dashboard", "")],
    "flag_regression": [("checkout", "new checkout flow failing", "new-checkout-flow"),
                        ("notifications", "duplicate reminder emails", "batched-email-sends"),
                        ("search", "irrelevant search results", "search-v2-ranking"),
                        ("reporting", "PDF invoices render blank", "pdf-renderer-v3")],
    "vendor_outage": [("notifications", "email provider outage", ""),
                      ("payments-api", "card processor timeouts", ""),
                      ("webhooks", "DNS failures for webhook targets", "")],
}
PLAN = (["bad_deploy"] * 6 + ["traffic_spike"] * 4 + ["stale_cache"] * 3
        + ["flag_regression"] * 4 + ["vendor_outage"] * 3)
RUNBOOK = {"bad_deploy": "rollback-deploy", "traffic_spike": "scale-service",
           "stale_cache": "flush-cache", "flag_regression": "toggle-flag", "vendor_outage": None}
AREAS = ("tax calculation", "session handling", "retry", "serializer", "connection pool")

INVESTIGATE = {
    "bad_deploy": ["Error rate on {service} went from 0.2% to {rate}% at {alert}.",
                   "Did anything ship to {service} in the last hour?",
                   "{version} went out at {cause}. The diff touches the {area} code.",
                   "Stack traces all point at the new {area} code path."],
    "traffic_spike": ["p99 on {service} is {latency} ms and climbing.",
                      "Request rate is 3.4x normal, mostly one tenant's batch job.",
                      "CPU is pinned at 95% on every {service} pod. We need more replicas.",
                      "Queue depth on {service} is 40k and growing."],
    "stale_cache": ["Users report seeing data from yesterday on {service}.",
                    "Cache hit rate looks normal, but the values are stale.",
                    "A cache key format change at {cause} left old entries in place.",
                    "Flushing the {service} cache should clear it."],
    "flag_regression": ["Errors on {service} started a few minutes ago.",
                        "Nothing deployed. Any flag changes?",
                        "{flag} was turned on at {cause}. Errors start right after.",
                        "Turning {flag} off is the fastest way back."],
    "vendor_outage": ["{service} calls to the provider are timing out.",
                      "The provider status page confirms a partial outage.",
                      "Our retries are holding. Nothing to roll back on our side.",
                      "Queueing outbound work until the provider recovers."],
}
ACTIONS = {
    "bad_deploy": ["#action add a canary stage for {service} deploys", "#action add a test "
                   "for the {area} change that broke"],
    "traffic_spike": ["#action set autoscaling limits for {service}", "#action add a per "
                      "tenant rate limit on {service}"],
    "stale_cache": ["#action version cache keys on {service}", "#action add a stale data "
                    "check to the {service} synthetic test"],
    "flag_regression": ["#action require a staged rollout for {flag}", "#action alert on "
                        "error rate per flag cohort"],
    "vendor_outage": ["#action evaluate a second provider for {service}", "#action show "
                      "provider status on the {service} dashboard"],
}


@dataclass
class SimResult:
    bot: IncidentBot
    clock: FakeClock
    incidents: list[Incident] = field(default_factory=list)


class Script:
    """Collects (time, action) pairs and replays them in time order on the bot."""

    def __init__(self, bot: IncidentBot, clock: FakeClock) -> None:
        self.bot, self.clock = bot, clock
        self.items: list[tuple[float, int, Callable[[], None]]] = []

    def at(self, ts: float, fn: Callable[[], None]) -> None:
        self.items.append((ts, len(self.items), fn))

    def run(self) -> None:
        for ts, _, fn in sorted(self.items, key=lambda x: (x[0], x[1])):
            while self.clock.now() + 60 <= ts and self.bot.open_incidents():
                self.clock.advance(60)
                self.bot.tick()
            self.clock.set(max(self.clock.now(), ts))
            fn()


def _users(rng: random.Random, n: int) -> list[str]:
    return rng.sample(USERS, n)


def schedule_incident(script: Script, rng: random.Random, kind: str, scenario: tuple,
                      severity: str, t_alert: float, versions: dict[str, int],
                      escalate: bool = False) -> dict[str, Any]:
    bot = script.bot
    service, title, flag = scenario
    ic, comms, ops, extra = _users(rng, 4)
    ctx: dict[str, Any] = {"inc": None}
    m = 60.0
    month = datetime.fromtimestamp(t_alert, UTC).month
    versions[service] = versions.get(service, 0) + 1
    version = f"v2026.{month}.{versions[service]}"
    t_cause = t_alert - rng.randint(2, 9) * m
    users = {"sev1": (3000, 12000), "sev2": (800, 4000), "sev3": (100, 900), "sev4": (5, 60)}
    affected = rng.randint(*users[severity])
    metric = "p99_latency" if kind == "traffic_spike" else "error_rate"
    value = rng.uniform(1800, 4200) if metric == "p99_latency" else rng.uniform(0.04, 0.3)
    threshold = 1200 if metric == "p99_latency" else 0.02
    fmt = {"service": service, "rate": f"{value * 100:.0f}", "latency": f"{value:.0f}",
           "version": version, "flag": flag, "area": rng.choice(AREAS),
           "alert": _hhmm(t_alert), "cause": _hhmm(t_cause)}

    def ch() -> str:
        return ctx["inc"].channel_id

    def cmd(user: str, text: str) -> Callable[[], None]:
        return lambda: bot.handle(SlashCommand(user, ch(), "/incident", text))

    def say(user: str, text: str, star_by: str | None = None) -> Callable[[], None]:
        def fn() -> None:
            bot.handle(MessagePosted(user, ch(), text.format(**fmt)))
            if star_by:
                msg = bot.store.messages(ch(), 1)[-1]
                bot.handle(ReactionAdded(star_by, ch(), msg.id, "star"))
        return fn

    def press(user: str, action_id: str, value_fn: Callable[[], str | None]) -> Callable:
        def fn() -> None:
            value = value_fn()
            if value:
                bot.handle(Interaction(user, ch(), "", action_id, value))
        return fn

    # bus events before the page
    if kind == "bad_deploy":
        script.at(t_cause, lambda: bot.bus.publish(deploy(t_cause, service, version, "ci")))
    elif kind == "flag_regression":
        script.at(t_cause, lambda: bot.bus.publish(flag_change(t_cause, service, flag, True,
                                                               extra)))
    elif kind == "stale_cache":
        script.at(t_cause, lambda: bot.bus.publish(deploy(t_cause, service, version, "ci")))
    script.at(t_alert, lambda: bot.bus.publish(alert(t_alert, service, metric, value, threshold,
                                                     affected)))

    tta = {"sev1": (1, 5), "sev2": (2, 9), "sev3": (4, 18), "sev4": (8, 30)}[severity]
    t_declare = t_alert + rng.randint(*tta) * m

    def declare() -> None:
        bot.handle(SlashCommand(ic, bot.incidents_channel.id, "/incident",
                                f'declare {severity} "{title}" service={service}'))
        ctx["inc"] = max(bot.incidents.values(), key=lambda i: i.declared_at)

    script.at(t_declare, declare)
    t = t_declare + 1 * m
    script.at(t, press(comms, "claim_role", lambda: "comms"))
    script.at(t + 30, cmd(ic, f"role ops @{ops}"))

    lines = INVESTIGATE[kind]
    t += rng.randint(2, 4) * m
    script.at(t, say(ops, lines[0]))
    script.at(t + 60, say(ic, lines[1]))
    t += rng.randint(3, 10) * m
    script.at(t, say(ops, lines[2], star_by=ic))
    t_identified = t + rng.randint(2, 12) * m
    if kind == "vendor_outage":
        t_identified += rng.randint(5, 20) * m
    script.at(t_identified - 30, say(extra, lines[3], star_by=rng.choice([ic, comms])))
    script.at(t_identified, cmd(ic, "state identified"))

    if escalate:
        script.at(t_identified + 60, cmd(ic, 'sev sev1 "impact is wider than first thought"'))

    rb = RUNBOOK[kind]
    t_mitigate = t_identified + rng.randint(4, 12) * m
    if rb:
        params = {"toggle-flag": f"flag={flag} enabled=false"}.get(rb, "")
        t_rb = t_identified + rng.randint(1, 4) * m
        if rng.random() < 0.35:
            script.at(t_rb - 60, cmd(ops, f"runbook {rb} {params} --dry-run".strip()))

        def waiting() -> str | None:
            inc = ctx["inc"]
            runs = [bot.executor.runs[r] for r in inc.runbook_runs]
            return next((r.id for r in runs if r.status == "awaiting_confirmation"), None)

        script.at(t_rb, cmd(ops, f"runbook {rb} {params}".strip()))
        script.at(t_rb + rng.randint(1, 3) * m, press(ops if rng.random() < 0.7 else ic,
                                                      "runbook_confirm", waiting))
        if rng.random() < 0.15:  # someone runs it again from scrollback: deduplicated
            script.at(t_rb + 4 * m, cmd(extra if extra in (ops, ic) else ops,
                                        f"runbook {rb} {params}".strip()))
        t_mitigate = max(t_mitigate, t_rb + 5 * m)
    else:
        t_mitigate = t_identified + rng.randint(25, 80) * m
    script.at(t_mitigate, cmd(ic, "state monitoring"))
    script.at(t_mitigate + 2 * m,
              lambda: bot.bus.publish(alert_resolved(script.clock.now(), service, metric)))
    t_resolve = t_mitigate + rng.randint(15, 40) * m

    # long incidents hand off the commander role halfway through
    handoff_to = extra
    if t_resolve - t_declare > 100 * m:
        t_ho = t_declare + (t_resolve - t_declare) / 2
        script.at(t_ho, cmd(ic, f"handoff commander @{handoff_to}"))
        closer = handoff_to
    else:
        closer = ic

    # comms cadence: mostly on time, sometimes late so reminders fire
    cadence = policy_for(severity).cadence_minutes or 120
    if escalate:
        cadence = 15
    tu = t_declare
    n = 0
    while True:
        tu += cadence * m * (rng.uniform(0.55, 0.95) if rng.random() < 0.8
                             else rng.uniform(1.05, 1.6))
        if tu >= t_resolve - 5 * m:
            break
        n += 1
        phase = ("investigating" if tu < t_identified else "identified" if tu < t_mitigate
                 else "monitoring")
        text = {"investigating": f"Investigating {title}. Next update soon.",
                "identified": f"Cause identified for {title}, working on mitigation.",
                "monitoring": "Mitigation applied. Error rates back to baseline, watching."}
        script.at(tu, cmd(comms, f'update "{text[phase]} ({n})"'))

    # public status: sev1 drafts are opened by the bot; comms approves each one
    def pending() -> str | None:
        p = bot.status.pending(ctx["inc"].id)
        return p[0].id if p else None

    public = severity == "sev1" or escalate or (severity == "sev2" and rng.random() < 0.5)
    if public:
        if severity != "sev1" and not escalate:
            script.at(t_declare + 4 * m, cmd(comms, "status"))
        script.at(t_declare + 5 * m, press(comms, "status_approve", pending))
        script.at(t_identified + 3 * m, cmd(comms, "status"))
        script.at(t_identified + 4 * m, press(comms, "status_approve", pending))
        script.at(t_mitigate + 1 * m, cmd(comms, "status"))
        script.at(t_mitigate + 2 * m, press(comms, "status_approve", pending))

    for i, line in enumerate(rng.sample(ACTIONS[kind], rng.randint(1, 2))):
        owner = rng.choice([ops, ic, extra])
        script.at(t_mitigate + (3 + i) * m, say(rng.choice([ic, ops]), f"{line} @{owner}"))
    if rng.random() < 0.5:
        script.at(t_mitigate + 6 * m, cmd(ic, f'action "write the postmortem for {title}" '
                                              f"owner=@{ic}"))

    script.at(t_resolve - 60, cmd(comms, f'update "Error rates have been at baseline for '
                                         f'{round((t_resolve - t_mitigate) / m)} minutes. '
                                         'Resolving."'))
    script.at(t_resolve, cmd(closer, "resolve"))
    if public:
        script.at(t_resolve + 2 * m, press(comms, "status_approve", pending))
    ctx.update(service=service, kind=kind, ic=ic, closer=closer, resolve_at=t_resolve)
    return ctx


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M")


def run_quarter(seed: int = 7, bot: IncidentBot | None = None,
                clock: FakeClock | None = None) -> SimResult:
    rng = random.Random(seed)
    clock = clock or FakeClock(QUARTER_START)
    clock.set(QUARTER_START)
    bot = bot or IncidentBot(clock=clock)
    script = Script(bot, clock)
    days = sorted(rng.sample(range(1, 89), len(PLAN)))
    kinds = list(PLAN)
    rng.shuffle(kinds)
    used: dict[str, int] = {}
    versions: dict[str, int] = {}
    severities = rng.choices(["sev1", "sev2", "sev3", "sev4"], [0.12, 0.45, 0.33, 0.10],
                             k=len(PLAN))
    severities[kinds.index("bad_deploy")] = "sev1"
    severities[kinds.index("vendor_outage")] = "sev1"
    contexts = []
    escalated = False
    for day, kind, sev in zip(days, kinds, severities, strict=True):
        options = SCENARIOS[kind]
        scenario = options[used.get(kind, 0) % len(options)]
        used[kind] = used.get(kind, 0) + 1
        hour = rng.choice([2, 9, 10, 11, 13, 14, 15, 16, 17, 19, 22])
        t_alert = QUARTER_START + day * 86400 + hour * 3600 + rng.randint(0, 59) * 60
        esc = sev == "sev2" and not escalated and day > 30
        escalated = escalated or esc
        noise_svc = rng.choice([s for s, _, _ in sum(SCENARIOS.values(), [])
                                if s != scenario[0]])
        t_noise = t_alert - rng.randint(20, 50) * 60
        versions[noise_svc] = versions.get(noise_svc, 0) + 1
        noise_ver = f"v2026.{datetime.fromtimestamp(t_noise, UTC).month}.{versions[noise_svc]}"
        script.at(t_noise, lambda t=t_noise, s=noise_svc, v=noise_ver:
                  bot.bus.publish(deploy(t, s, v, "ci")))
        contexts.append(schedule_incident(script, rng, kind, scenario, sev, t_alert, versions,
                                          escalate=esc))
    # action item follow-through: owners close most items within three weeks
    for ctx in contexts:
        for k in range(3):
            if rng.random() < 0.7:
                ts = min(QUARTER_END - 3600, ctx["resolve_at"] + rng.uniform(1, 21) * 86400)
                script.at(ts, lambda c=ctx, k=k: _close_item(bot, c, k))
    script.run()
    clock.set(max(clock.now(), QUARTER_END))
    return SimResult(bot, clock, [c["inc"] for c in contexts])


def _close_item(bot: IncidentBot, ctx: dict[str, Any], k: int) -> None:
    inc = ctx["inc"]
    if k >= len(inc.action_items) or inc.action_items[k].done:
        return
    item = inc.action_items[k]
    bot.handle(SlashCommand(item.owner or ctx["ic"], inc.channel_id, "/incident",
                            f"done {item.id}"))


REQUIRED_KINDS = ("declare", "role", "alert", "state", "update")


def check(incidents: list[Incident]) -> list[str]:
    """Problems that would make the simulation untrustworthy. Empty means it passed."""
    problems = []
    for inc in incidents:
        kinds = {e.kind for e in inc.timeline}
        if inc.state != "resolved":
            problems.append(f"{inc.id} is {inc.state}, not resolved")
        missing = [k for k in REQUIRED_KINDS if k not in kinds]
        if missing:
            problems.append(f"{inc.id} timeline is missing {', '.join(missing)}")
        states = [e.data.get("new") for e in inc.timeline if e.kind == "state"]
        if "monitoring" not in states or "resolved" not in states:
            problems.append(f"{inc.id} never reached monitoring and resolved")
        if not all(inc.roles.values()):
            problems.append(f"{inc.id} has unfilled roles {inc.roles}")
        if not (inc.detected_at <= inc.declared_at <= (inc.mitigated_at or 0)
                <= (inc.resolved_at or 0)):
            problems.append(f"{inc.id} timestamps are out of order")
        pm = inc.postmortem
        if pm is None:
            problems.append(f"{inc.id} has no postmortem draft")
        else:
            ids = {e.id for e in inc.timeline}
            errs = validate_narrative({k: pm[k] for k in (
                "summary", "summary_citations", "contributing_factors", "what_went_well",
                "what_went_poorly", "suggested_action_items")}, ids)
            if errs or pm["status"] != "draft":
                problems.append(f"{inc.id} postmortem problems: {errs}")
    return problems


def seed_live(bot: IncidentBot, clock: FakeClock, start: float) -> Incident:
    """An incident in progress for the demo: a bad deploy to checkout, a rollback waiting for
    confirmation. Times are relative to `start` (about 25 minutes before now)."""
    m = 60.0
    bot.systems.versions["checkout"] = ["v2026.11.1", "v2026.11.2"]
    clock.set(start)
    bot.bus.publish(deploy(start, "checkout", "v2026.11.3", "ci"))
    clock.set(start + 3 * m)
    bot.bus.publish(alert(start + 3 * m, "checkout", "error_rate", 0.124, 0.02, 4210))
    steps: list[tuple[float, Any]] = [
        (5, SlashCommand("hiro", bot.incidents_channel.id, "/incident",
                         'declare sev2 "checkout errors" service=checkout')),
    ]
    for delay, ev in steps:
        clock.set(start + delay * m)
        bot.handle(ev)
    inc = max(bot.incidents.values(), key=lambda i: i.declared_at)
    ch = inc.channel_id
    script = [
        (6, Interaction("grace", ch, "", "claim_role", "comms")),
        (6.5, SlashCommand("hiro", ch, "/incident", "role ops @kofi")),
        (8, MessagePosted("kofi", ch, "Checkout 5xx went from 0.2% to 12% at "
                          f"{_hhmm(start + 3 * m)}. All regions.")),
        (9, MessagePosted("hiro", ch, "Did anything ship to checkout in the last hour?")),
        (10, MessagePosted("kofi", ch, f"v2026.11.3 went out at {_hhmm(start)}. The diff "
                           "touches tax calculation for EU invoices.")),
        (12, SlashCommand("grace", ch, "/incident", 'update "Checkout errors for about 12% '
                          'of requests. Suspect deploy v2026.11.3. Next update in 30 min."')),
        (13, SlashCommand("hiro", ch, "/incident", "state identified")),
        (14, MessagePosted("ana", ch, "#action add a canary stage for checkout deploys "
                           "@kofi")),
        (15, SlashCommand("grace", ch, "/incident", "status")),
        (16, SlashCommand("kofi", ch, "/incident", "runbook rollback-deploy --dry-run")),
        (17, SlashCommand("kofi", ch, "/incident", "runbook rollback-deploy")),
    ]
    for delay, ev in script:
        clock.set(start + delay * m)
        bot.handle(ev)
        if delay == 10:
            msg = bot.store.messages(ch, 1)[-1]
            bot.handle(ReactionAdded("hiro", ch, msg.id, "star"))
    return inc
