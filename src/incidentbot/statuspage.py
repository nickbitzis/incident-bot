"""Internal and public status components with templated updates.

Internal updates publish at once. Public updates are drafts until the incident's
communications lead approves them; only then do public components change status.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

COMPONENTS: dict[str, tuple[str, ...]] = {
    "Dashboard": ("web-app",),
    "Login and SSO": ("auth",),
    "Invoicing": ("invoices-api",),
    "Payments and checkout": ("payments-api", "checkout"),
    "Search": ("search",),
    "Email notifications": ("notifications",),
    "Webhooks and API": ("webhooks",),
    "Reports and exports": ("reporting",),
}
STATUS_LEVELS = ("operational", "degraded_performance", "partial_outage", "major_outage")

TEMPLATES = {
    "investigating": "We are investigating {impact} affecting {component}. "
                     "We will post another update within {next_minutes} minutes.",
    "identified": "We have identified the cause of {impact} affecting {component} and are "
                  "working on a fix. Next update within {next_minutes} minutes.",
    "monitoring": "A fix is in place for {impact} affecting {component}. "
                  "We are monitoring the results.",
    "resolved": "This incident is resolved. {component} is operating normally. "
                "Thank you for your patience.",
}


class ApprovalError(PermissionError):
    pass


def component_for(service: str) -> str:
    return next((c for c, svcs in COMPONENTS.items() if service in svcs), "Platform")


@dataclass
class StatusUpdate:
    id: str
    status_incident_id: str
    visibility: str  # internal or public
    state: str
    body: str
    author: str
    created_at: float
    status: str = "pending_approval"  # pending_approval, published, rejected
    approved_by: str | None = None
    published_at: float | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StatusIncident:
    id: str
    incident_id: str
    title: str
    component: str
    level: str  # the public status this incident sets while unresolved
    state: str = "investigating"
    public_state: str | None = None  # last published public state; None means not public yet
    opened_at: float = 0.0
    updates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StatusPage:
    def __init__(self) -> None:
        self.components = dict.fromkeys(COMPONENTS, "operational")
        self.internal: dict[str, str] = {}
        self.incidents: dict[str, StatusIncident] = {}
        self.updates: dict[str, StatusUpdate] = {}

    def open(self, incident_id: str, title: str, service: str, level: str,
             ts: float) -> StatusIncident:
        existing = next((s for s in self.incidents.values() if s.incident_id == incident_id),
                        None)
        if existing:
            existing.level = _worse(existing.level, level)
            return existing
        si = StatusIncident(f"SP-{len(self.incidents) + 1:03d}", incident_id, title,
                            component_for(service), level, opened_at=ts)
        self.incidents[si.id] = si
        return si

    def set_internal(self, service: str, level: str) -> None:
        self.internal[service] = level

    def render(self, state: str, title: str, component: str, next_minutes: int | None) -> str:
        return TEMPLATES[state].format(impact=title.lower(), component=component,
                                       next_minutes=next_minutes or 60)

    def _new_update(self, si: StatusIncident, visibility: str, state: str, body: str,
                    author: str, ts: float) -> StatusUpdate:
        u = StatusUpdate(f"SU-{len(self.updates) + 1:03d}", si.id, visibility, state, body,
                         author, ts)
        self.updates[u.id] = u
        si.updates.append(u.id)
        return u

    def post_internal(self, si: StatusIncident, state: str, body: str, author: str,
                      ts: float) -> StatusUpdate:
        u = self._new_update(si, "internal", state, body, author, ts)
        u.status, u.published_at = "published", ts
        si.state = state
        return u

    def draft_public(self, si: StatusIncident, state: str, body: str, author: str,
                     ts: float) -> StatusUpdate:
        return self._new_update(si, "public", state, body, author, ts)

    def approve(self, update_id: str, approver: str, comms_lead: str | None,
                ts: float) -> StatusUpdate:
        u = self._get(update_id)
        if u.status != "pending_approval":
            raise ApprovalError(f"{u.id} is already {u.status.replace('_', ' ')}")
        if comms_lead is None or approver != comms_lead:
            who = f"@{comms_lead}" if comms_lead else "the communications lead (none assigned)"
            raise ApprovalError(f"only {who} can approve public updates")
        u.status, u.approved_by, u.published_at = "published", approver, ts
        si = self.incidents[u.status_incident_id]
        si.public_state = u.state
        self._recompute()
        return u

    def reject(self, update_id: str, approver: str, comms_lead: str | None,
               reason: str, ts: float) -> StatusUpdate:
        u = self._get(update_id)
        if u.status != "pending_approval":
            raise ApprovalError(f"{u.id} is already {u.status.replace('_', ' ')}")
        if comms_lead is None or approver != comms_lead:
            raise ApprovalError("only the communications lead can reject public updates")
        u.status, u.approved_by, u.note = "rejected", approver, reason
        return u

    def _get(self, update_id: str) -> StatusUpdate:
        if update_id.upper() not in self.updates:
            raise ApprovalError(f"no status update {update_id}")
        return self.updates[update_id.upper()]

    def _recompute(self) -> None:
        comps = dict.fromkeys(COMPONENTS, "operational")
        for si in self.incidents.values():
            if si.public_state in (None, "resolved") or si.component not in comps:
                continue
            level = "degraded_performance" if si.public_state == "monitoring" else si.level
            comps[si.component] = _worse(comps[si.component], level)
        self.components = comps

    def pending(self, incident_id: str | None = None) -> list[StatusUpdate]:
        return [u for u in self.updates.values() if u.status == "pending_approval" and (
            incident_id is None or self.incidents[u.status_incident_id].incident_id
            == incident_id)]

    def public_view(self, limit: int = 8) -> dict[str, Any]:
        shown = []
        for si in sorted(self.incidents.values(), key=lambda s: -s.opened_at):
            ups = [self.updates[u].to_dict() for u in si.updates
                   if self.updates[u].visibility == "public"
                   and self.updates[u].status == "published"]
            if ups:
                shown.append({"id": si.id, "title": si.title, "component": si.component,
                              "state": si.public_state, "opened_at": si.opened_at,
                              "updates": list(reversed(ups))})
            if len(shown) >= limit:
                break
        overall = max(self.components.values(), key=STATUS_LEVELS.index)
        return {"overall": overall,
                "components": [{"name": n, "status": s} for n, s in self.components.items()],
                "incidents": shown}

    def internal_view(self) -> dict[str, Any]:
        return {"services": dict(sorted(self.internal.items())),
                "incidents": [si.to_dict() for si in self.incidents.values()],
                "updates": [u.to_dict() for u in self.updates.values()]}


def _worse(a: str, b: str) -> str:
    return a if STATUS_LEVELS.index(a) >= STATUS_LEVELS.index(b) else b
