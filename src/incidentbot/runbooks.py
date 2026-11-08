"""Runbooks as YAML, executed step by step with confirmation, dry-run, idempotency, and audit.

A run plans each step against the fake systems before doing anything. Steps marked
`confirm: true` stop and wait for a button press from the ops lead or the incident commander.
A run is keyed by (incident, runbook, params): starting the same run again returns the first
one instead of rolling back twice. `--force` starts a fresh run on purpose.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from incidentbot.systems import ActionError, FakeSystems

RUNBOOK_DIR = Path(__file__).parent / "runbooks"


class RunbookError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    id: str
    description: str
    action: str
    args: dict[str, str]
    confirm: bool = False


@dataclass(frozen=True)
class Runbook:
    name: str
    title: str
    description: str
    params: dict[str, dict[str, Any]]
    steps: tuple[Step, ...]

    def resolve_params(self, given: dict[str, str]) -> dict[str, str]:
        unknown = set(given) - set(self.params)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise RunbookError(f"unknown parameter(s) for {self.name}: {names}")
        out: dict[str, str] = {}
        for name, spec in self.params.items():
            if name in given and given[name] != "":
                out[name] = str(given[name])
            elif "default" in spec:
                out[name] = str(spec["default"])
            elif spec.get("required"):
                raise RunbookError(f"{self.name} needs {name}=...")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "description": self.description,
                "params": self.params, "steps": [asdict(s) for s in self.steps]}


def parse_runbook(doc: dict[str, Any]) -> Runbook:
    for key in ("name", "title", "steps"):
        if key not in doc:
            raise RunbookError(f"runbook is missing {key!r}")
    steps = []
    seen: set[str] = set()
    for i, s in enumerate(doc["steps"]):
        if "action" not in s or "id" not in s:
            raise RunbookError(f"{doc['name']} step {i + 1} needs id and action")
        if s["id"] in seen:
            raise RunbookError(f"{doc['name']} has duplicate step id {s['id']!r}")
        seen.add(s["id"])
        steps.append(Step(s["id"], s.get("description", s["action"]), s["action"],
                          {k: str(v) for k, v in (s.get("args") or {}).items()},
                          bool(s.get("confirm", False))))
    return Runbook(doc["name"], doc["title"], doc.get("description", ""),
                   doc.get("params") or {}, tuple(steps))


def load_runbooks(directory: Path = RUNBOOK_DIR) -> dict[str, Runbook]:
    books = {}
    for path in sorted(directory.glob("*.yaml")):
        rb = parse_runbook(yaml.safe_load(path.read_text()))
        books[rb.name] = rb
    return books


@dataclass
class StepRun:
    id: str
    description: str
    action: str
    confirm: bool
    status: str = "pending"  # pending, awaiting_confirmation, done, planned, failed, cancelled
    args: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    result: str = ""
    changed: bool = False
    ts: float | None = None
    by: str | None = None


@dataclass
class RunbookRun:
    id: str
    runbook: str
    incident_id: str
    params: dict[str, str]
    actor: str
    dry_run: bool
    key: str
    created_at: float
    status: str = "running"  # running, awaiting_confirmation, completed, failed, cancelled, dry_run
    steps: list[StepRun] = field(default_factory=list)
    finished_at: float | None = None
    error: str = ""

    def current(self) -> StepRun | None:
        return next((s for s in self.steps if s.status in ("pending", "awaiting_confirmation")),
                    None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditEntry:
    ts: float
    run_id: str
    incident_id: str
    step_id: str
    action: str
    actor: str
    args: dict[str, Any]
    dry_run: bool
    outcome: str  # planned, applied, no_change, failed, cancelled, reused
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def idempotency_key(incident_id: str, runbook: str, params: dict[str, str]) -> str:
    raw = json.dumps([incident_id, runbook, sorted(params.items())])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class RunbookExecutor:
    def __init__(self, systems: FakeSystems, runbooks: dict[str, Runbook] | None = None) -> None:
        self.systems = systems
        self.runbooks = runbooks if runbooks is not None else load_runbooks()
        self.runs: dict[str, RunbookRun] = {}
        self.by_key: dict[str, str] = {}
        self.audit: list[AuditEntry] = []

    def _audit(self, ts, run: RunbookRun, step: StepRun | None, actor, outcome, detail):
        self.audit.append(AuditEntry(ts, run.id, run.incident_id, step.id if step else "-",
                                     step.action if step else "-", actor,
                                     dict(step.args) if step else dict(run.params),
                                     run.dry_run, outcome, detail))

    def get(self, run_id: str) -> RunbookRun:
        if run_id not in self.runs:
            raise RunbookError(f"no runbook run {run_id}")
        return self.runs[run_id]

    def start(self, name: str, params: dict[str, str], incident_id: str, actor: str, ts: float,
              dry_run: bool = False, force: bool = False) -> tuple[RunbookRun, bool]:
        """Returns (run, reused). A reused run is the earlier run with the same key."""
        if name not in self.runbooks:
            raise RunbookError(f"no runbook {name!r}. Known: {', '.join(sorted(self.runbooks))}")
        rb = self.runbooks[name]
        resolved = rb.resolve_params(params)
        key = idempotency_key(incident_id, name, resolved)
        if not dry_run and not force and key in self.by_key:
            prior = self.runs[self.by_key[key]]
            if prior.status not in ("failed", "cancelled"):
                self._audit(ts, prior, None, actor, "reused", "same runbook and parameters")
                return prior, True
        run = RunbookRun(f"RB-{len(self.runs) + 1:03d}", name, incident_id, resolved, actor,
                         dry_run, key, ts)
        for s in rb.steps:
            try:
                args = {k: v.format_map(resolved) for k, v in s.args.items()}
            except KeyError as exc:
                raise RunbookError(f"step {s.id} uses unknown parameter {exc}") from exc
            run.steps.append(StepRun(s.id, s.description, s.action, s.confirm, args=args))
        self.runs[run.id] = run
        if not dry_run:
            self.by_key[key] = run.id
        self._advance(run, actor, ts)
        return run, False

    def _advance(self, run: RunbookRun, actor: str, ts: float) -> None:
        while (step := run.current()) is not None:
            try:
                plan = self.systems.plan(step.action, step.args)
            except ActionError as exc:
                self._fail(run, step, actor, ts, str(exc))
                return
            step.args, step.summary = plan.args, plan.summary
            if run.dry_run:
                step.status, step.changed, step.ts = "planned", plan.changes, ts
                step.result = f"dry run: would {plan.summary}" if plan.changes else (
                    f"dry run: {plan.summary}")
                self._audit(ts, run, step, actor, "planned", step.result)
                continue
            if step.confirm and step.status == "pending" and plan.changes:
                step.status = "awaiting_confirmation"
                run.status = "awaiting_confirmation"
                return
            self._apply(run, step, actor, ts, plan)
            if run.status == "failed":
                return
        run.status = "dry_run" if run.dry_run else "completed"
        run.finished_at = ts

    def _apply(self, run, step, actor, ts, plan) -> None:
        try:
            result = self.systems.apply(plan)
        except ActionError as exc:
            self._fail(run, step, actor, ts, str(exc))
            return
        step.status, step.result, step.changed, step.ts, step.by = (
            "done", result, plan.changes, ts, actor)
        run.status = "running"
        self._audit(ts, run, step, actor, "applied" if plan.changes else "no_change", result)

    def _fail(self, run, step, actor, ts, msg) -> None:
        step.status, step.result, step.ts = "failed", msg, ts
        run.status, run.error, run.finished_at = "failed", msg, ts
        self._audit(ts, run, step, actor, "failed", msg)

    def confirm(self, run_id: str, actor: str, ts: float) -> tuple[RunbookRun, bool]:
        """Apply the step waiting for confirmation. Returns (run, acted). A second press of the
        same button finds nothing waiting and does nothing."""
        run = self.get(run_id)
        step = run.current()
        if run.status != "awaiting_confirmation" or step is None or (
                step.status != "awaiting_confirmation"):
            return run, False
        try:
            plan = self.systems.plan(step.action, step.args)  # re-plan: the world may have moved
        except ActionError as exc:
            self._fail(run, step, actor, ts, str(exc))
            return run, True
        step.summary = plan.summary
        self._apply(run, step, actor, ts, plan)
        if run.status != "failed":
            self._advance(run, actor, ts)
        return run, True

    def cancel(self, run_id: str, actor: str, ts: float) -> tuple[RunbookRun, bool]:
        run = self.get(run_id)
        if run.status != "awaiting_confirmation":
            return run, False
        for s in run.steps:
            if s.status in ("pending", "awaiting_confirmation"):
                s.status = "cancelled"
        run.status, run.finished_at = "cancelled", ts
        self._audit(ts, run, None, actor, "cancelled", "cancelled from chat")
        return run, True
