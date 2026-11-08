"""FastAPI service: chat events (slash commands, messages, reactions, buttons), a Slack-shaped
events endpoint, incidents, status page, metrics, runbooks, the fake event bus, and the UI.

At startup the service replays the scripted quarter (20 incidents, about 0.1 s) so the
dashboard has data, then opens one live incident that is waiting on a rollback confirmation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from incidentbot import eventbus
from incidentbot.bot import IncidentBot
from incidentbot.chat import (
    ChatStore,
    InMemoryChatStore,
    Interaction,
    MessagePosted,
    ReactionAdded,
    SlashCommand,
    SqliteChatStore,
)
from incidentbot.chat.slack import UnsupportedPayload, from_slack, parse_form
from incidentbot.clock import DemoClock
from incidentbot.logs import log, setup_logging
from incidentbot.metrics import summarize
from incidentbot.postmortem import ClaudeWriter, PostmortemWriter, TemplateWriter, to_markdown
from incidentbot.simulation import QUARTER_START, run_quarter, seed_live
from incidentbot.systems import SERVICES

WEB = Path(__file__).parent / "web"
logger = logging.getLogger("incidentbot.api")


class CommandIn(BaseModel):
    user: str = Field(min_length=1, max_length=40)
    channel_id: str
    text: str = Field(max_length=2000)
    command: str = "/incident"


class MessageIn(BaseModel):
    user: str = Field(min_length=1, max_length=40)
    channel_id: str
    text: str = Field(min_length=1, max_length=4000)
    thread_ts: str | None = None


class ReactionIn(BaseModel):
    user: str = Field(min_length=1, max_length=40)
    channel_id: str
    message_id: str
    reaction: str = "star"


class InteractionIn(BaseModel):
    user: str = Field(min_length=1, max_length=40)
    channel_id: str
    message_id: str = ""
    action_id: str
    value: str


class DeployIn(BaseModel):
    service: str
    version: str
    author: str = "ci"


class AlertIn(BaseModel):
    service: str
    metric: str = "error_rate"
    value: float = 0.08
    threshold: float = 0.02
    affected_users: int = 500


def make_writer() -> PostmortemWriter:
    if os.environ.get("INCIDENTBOT_WRITER", "template").lower() == "claude":
        return ClaudeWriter()
    return TemplateWriter()


def make_store() -> ChatStore:
    path = os.environ.get("INCIDENTBOT_DB", "").strip()
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if Path(path).exists():
            Path(path).unlink()  # the demo reseeds on every start
        return SqliteChatStore(path)
    return InMemoryChatStore()


def build_demo(store: ChatStore | None = None, writer: PostmortemWriter | None = None,
               live: bool = True, seed: int = 7) -> IncidentBot:
    clock = DemoClock(QUARTER_START)
    # The quarter always uses the offline writer: 20 drafts should not cost API calls.
    bot = IncidentBot(store=store or InMemoryChatStore(), clock=clock, writer=TemplateWriter())
    run_quarter(seed=seed, bot=bot, clock=clock)
    if live:
        seed_live(bot, clock, time.time() - 20 * 60)
    clock.release()
    bot.writer = writer or TemplateWriter()
    return bot


class HttpMetrics:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: dict[tuple[str, int], int] = {}
        self.seconds = 0.0
        self.count = 0

    def observe(self, route: str, code: int, seconds: float) -> None:
        with self.lock:
            self.requests[(route, code)] = self.requests.get((route, code), 0) + 1
            self.seconds += seconds
            self.count += 1


def render_prometheus(bot: IncidentBot, http: HttpMetrics) -> str:
    out = ["# HELP incidentbot_http_requests_total HTTP requests by route and status.",
           "# TYPE incidentbot_http_requests_total counter"]
    with http.lock:
        for (route, code), n in sorted(http.requests.items()):
            out.append(f'incidentbot_http_requests_total{{route="{route}",code="{code}"}} {n}')
        out += ["# HELP incidentbot_http_request_seconds Request latency.",
                "# TYPE incidentbot_http_request_seconds summary",
                f"incidentbot_http_request_seconds_sum {http.seconds:.6f}",
                f"incidentbot_http_request_seconds_count {http.count}"]
    with bot.lock:
        out += ["# HELP incidentbot_incidents Incidents by severity and state.",
                "# TYPE incidentbot_incidents gauge"]
        counts: dict[tuple[str, str], int] = {}
        for i in bot.incidents.values():
            counts[(i.severity, i.state)] = counts.get((i.severity, i.state), 0) + 1
        for (sev, state), n in sorted(counts.items()):
            out.append(f'incidentbot_incidents{{severity="{sev}",state="{state}"}} {n}')
        out += ["# HELP incidentbot_open_incidents Incidents not yet resolved.",
                "# TYPE incidentbot_open_incidents gauge",
                f"incidentbot_open_incidents {len(bot.open_incidents())}"]
        names = {"events": ("incidentbot_chat_events_total", ("type",)),
                 "commands": ("incidentbot_commands_total", ("verb", "outcome")),
                 "bus_events": ("incidentbot_bus_events_total", ("kind",)),
                 "reminders": ("incidentbot_update_reminders_total", ("severity",)),
                 "runbook_runs": ("incidentbot_runbook_runs_total", ("runbook", "mode")),
                 "runbook_steps": ("incidentbot_runbook_steps_total", ("action", "status"))}
        for key, (metric, labels) in names.items():
            out += [f"# HELP {metric} Counter of {key.replace('_', ' ')}.",
                    f"# TYPE {metric} counter"]
            for k, n in sorted(bot.counters.items()):
                if k[0] == key:
                    lbl = ",".join(f'{a}="{b}"' for a, b in zip(labels, k[1:], strict=False))
                    out.append(f"{metric}{{{lbl}}} {n}")
    return "\n".join(out) + "\n"


def create_app(bot: IncidentBot | None = None, tick_seconds: float = 30.0) -> FastAPI:
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    state: dict[str, Any] = {"bot": bot}
    http = HttpMetrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["bot"] is None:
            t0 = time.perf_counter()
            state["bot"] = build_demo(store=make_store(), writer=make_writer())
            log(logger, "demo data ready", seconds=round(time.perf_counter() - t0, 3),
                incidents=len(state["bot"].incidents))

        async def ticker():
            while True:
                await asyncio.sleep(tick_seconds)
                state["bot"].tick()

        task = asyncio.create_task(ticker())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app = FastAPI(title="incidentbot", version="1.0.0", lifespan=lifespan)

    def b() -> IncidentBot:
        if state["bot"] is None:
            raise HTTPException(503, "starting")
        return state["bot"]

    @app.middleware("http")
    async def timing(request: Request, call_next):
        t0 = time.perf_counter()
        resp = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", "other")
        http.observe(path, resp.status_code, time.perf_counter() - t0)
        if path.startswith("/api") or path.startswith("/slack"):
            log(logger, "request", method=request.method, path=path, code=resp.status_code,
                ms=round((time.perf_counter() - t0) * 1000, 1))
        return resp

    def posted(msgs) -> dict[str, Any]:
        return {"ok": True, "messages": [m.to_dict() for m in msgs]}

    def check_channel(channel_id: str) -> None:
        if b().store.get_channel(channel_id) is None:
            raise HTTPException(404, f"no channel {channel_id}")

    # chat -------------------------------------------------------------------------
    @app.post("/api/chat/command")
    def chat_command(body: CommandIn):
        check_channel(body.channel_id)
        text = body.text.strip()
        if text.startswith(body.command):
            text = text[len(body.command):].strip()
        return posted(b().handle(SlashCommand(body.user.lower(), body.channel_id,
                                              body.command, text)))

    @app.post("/api/chat/message")
    def chat_message(body: MessageIn):
        check_channel(body.channel_id)
        text = body.text.strip()
        if text.startswith("/"):
            cmd, _, rest = text.partition(" ")
            return posted(b().handle(SlashCommand(body.user.lower(), body.channel_id, cmd,
                                                  rest)))
        return posted(b().handle(MessagePosted(body.user.lower(), body.channel_id, text,
                                               body.thread_ts)))

    @app.post("/api/chat/reaction")
    def chat_reaction(body: ReactionIn):
        if b().store.get_message(body.message_id) is None:
            raise HTTPException(404, f"no message {body.message_id}")
        return posted(b().handle(ReactionAdded(body.user.lower(), body.channel_id,
                                               body.message_id, body.reaction)))

    @app.post("/api/chat/interaction")
    def chat_interaction(body: InteractionIn):
        check_channel(body.channel_id)
        return posted(b().handle(Interaction(body.user.lower(), body.channel_id,
                                             body.message_id, body.action_id, body.value)))

    @app.post("/slack/events")
    async def slack_events(request: Request):
        raw = await request.body()
        ctype = request.headers.get("content-type", "")
        try:
            payload = parse_form(raw) if "form" in ctype else await request.json()
        except ValueError as exc:
            raise HTTPException(400, "body must be JSON or form encoded") from exc
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}
        try:
            events = from_slack(payload)
        except (UnsupportedPayload, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        msgs = []
        for ev in events:
            msgs += b().handle(ev)
        return posted(msgs)

    @app.get("/api/channels")
    def channels():
        bot = b()
        out = []
        for c in bot.store.list_channels():
            inc = bot.incidents.get(c.incident_id) if c.incident_id else None
            out.append({**c.to_dict(), "state": inc.state if inc else None,
                        "severity": inc.severity if inc else None})
        return {"channels": out}

    @app.get("/api/channels/{channel_id}/messages")
    def messages(channel_id: str, limit: int = 200):
        check_channel(channel_id)
        return {"messages": [m.to_dict() for m in b().store.messages(channel_id,
                                                                      min(limit, 500))]}

    # incidents --------------------------------------------------------------------
    @app.get("/api/incidents")
    def incidents(state: str | None = None, severity: str | None = None,
                  service: str | None = None):
        rows = [i.summary() for i in sorted(b().incidents.values(),
                                            key=lambda i: -i.declared_at)]
        rows = [r for r in rows if (state is None or r["state"] == state)
                and (severity is None or r["severity"] == severity)
                and (service is None or r["service"] == service)]
        return {"incidents": rows}

    def incident_or_404(incident_id: str):
        try:
            return b().get(incident_id)
        except KeyError:
            raise HTTPException(404, f"no incident {incident_id}") from None

    @app.get("/api/incidents/{incident_id}")
    def incident(incident_id: str):
        inc = incident_or_404(incident_id)
        bot = b()
        runs = [bot.executor.runs[r].to_dict() for r in inc.runbook_runs]
        audit = [a.to_dict() for a in bot.executor.audit if a.incident_id == inc.id]
        status = []
        if inc.status_incident_id:
            si = bot.status.incidents[inc.status_incident_id]
            status = [bot.status.updates[u].to_dict() for u in si.updates]
        return {**inc.to_dict(), "runbook_runs": runs, "audit": audit, "status_updates": status}

    @app.get("/api/incidents/{incident_id}/postmortem")
    def postmortem(incident_id: str, format: str = "json"):
        inc = incident_or_404(incident_id)
        if inc.postmortem is None:
            raise HTTPException(409, f"{inc.id} is not resolved yet")
        if format == "markdown":
            return PlainTextResponse(to_markdown(inc.postmortem), media_type="text/markdown")
        return inc.postmortem

    # status page ------------------------------------------------------------------
    @app.get("/api/status")
    def status_public():
        return b().status.public_view()

    @app.get("/api/status/internal")
    def status_internal():
        return b().status.internal_view()

    # metrics, runbooks, bus -------------------------------------------------------
    @app.get("/api/metrics/summary")
    def metrics_summary():
        with b().lock:
            return summarize(b().incidents.values())

    @app.get("/api/runbooks")
    def runbooks():
        bot = b()
        return {"runbooks": [rb.to_dict() for rb in bot.executor.runbooks.values()],
                "audit": [a.to_dict() for a in bot.executor.audit[-50:]]}

    @app.post("/api/bus/deploy")
    def bus_deploy(body: DeployIn):
        if body.service not in SERVICES:
            raise HTTPException(422, f"unknown service {body.service}")
        e = b().bus.publish(eventbus.deploy(b().now(), body.service, body.version, body.author))
        return e.to_dict()

    @app.post("/api/bus/alert")
    def bus_alert(body: AlertIn):
        if body.service not in SERVICES:
            raise HTTPException(422, f"unknown service {body.service}")
        e = b().bus.publish(eventbus.alert(b().now(), body.service, body.metric, body.value,
                                           body.threshold, body.affected_users))
        return e.to_dict()

    @app.post("/api/tick")
    def tick():
        return posted(b().tick())

    @app.get("/healthz")
    def healthz():
        bot = state["bot"]
        if bot is None:
            return JSONResponse({"status": "starting"}, status_code=503)
        return {"status": "ok", "incidents": len(bot.incidents),
                "open": len(bot.open_incidents()), "writer": bot.writer.name}

    @app.get("/metrics")
    def metrics():
        return PlainTextResponse(render_prometheus(b(), http),
                                 media_type="text/plain; version=0.0.4")

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app
