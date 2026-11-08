"""Incident domain model: severities, states, roles, timeline entries, action items."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITIES = ("sev1", "sev2", "sev3", "sev4")
STATES = ("investigating", "identified", "monitoring", "resolved")

# Allowed state changes. Resolving needs a cause or a mitigation first; resolved is final.
TRANSITIONS: dict[str, set[str]] = {
    "investigating": {"identified", "monitoring"},
    "identified": {"investigating", "monitoring", "resolved"},
    "monitoring": {"investigating", "identified", "resolved"},
    "resolved": set(),
}

ROLES = ("commander", "comms", "ops")
ROLE_TITLES = {
    "commander": "Incident commander",
    "comms": "Communications lead",
    "ops": "Ops lead",
}
ROLE_ALIASES = {
    "ic": "commander", "commander": "commander", "incident-commander": "commander",
    "comms": "comms", "cl": "comms", "communications": "comms", "comms-lead": "comms",
    "ops": "ops", "ol": "ops", "operations": "ops", "ops-lead": "ops",
}


class IncidentError(ValueError):
    """A command that is well formed but not allowed right now. The message goes to chat."""


@dataclass
class TimelineEntry:
    id: str
    ts: float
    kind: str  # declare, role, severity, state, update, status, message, alert, deploy, ...
    actor: str
    text: str
    source: str = "chat"  # chat, bus, bot
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ActionItem:
    id: str
    text: str
    owner: str | None
    created_at: float
    source_entry_id: str
    done: bool = False
    done_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Incident:
    id: str
    title: str
    severity: str
    service: str
    channel_id: str
    declared_by: str
    declared_at: float
    detected_at: float
    state: str = "investigating"
    roles: dict[str, str | None] = field(default_factory=lambda: dict.fromkeys(ROLES))
    identified_at: float | None = None
    mitigated_at: float | None = None
    resolved_at: float | None = None
    last_update_at: float = 0.0
    updates: list[float] = field(default_factory=list)
    reminders: list[float] = field(default_factory=list)
    timeline: list[TimelineEntry] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    pages: list[dict[str, Any]] = field(default_factory=list)
    status_incident_id: str | None = None
    runbook_runs: list[str] = field(default_factory=list)
    affected_users: int = 0
    postmortem: dict[str, Any] | None = None
    _seq: int = 0

    @property
    def is_open(self) -> bool:
        return self.state != "resolved"

    def add(self, ts: float, kind: str, actor: str, text: str, source: str = "chat",
            **data: Any) -> TimelineEntry:
        self._seq += 1
        entry = TimelineEntry(f"T{self._seq}", ts, kind, actor, text, source, data)
        self.timeline.append(entry)
        return entry

    def sorted_timeline(self) -> list[TimelineEntry]:
        return sorted(self.timeline, key=lambda e: (e.ts, int(e.id[1:])))

    def entry(self, entry_id: str) -> TimelineEntry | None:
        return next((e for e in self.timeline if e.id == entry_id), None)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "severity": self.severity,
            "service": self.service, "state": self.state, "channel_id": self.channel_id,
            "declared_at": self.declared_at, "detected_at": self.detected_at,
            "identified_at": self.identified_at, "mitigated_at": self.mitigated_at,
            "resolved_at": self.resolved_at, "roles": dict(self.roles),
            "affected_users": self.affected_users,
            "action_items_open": sum(not a.done for a in self.action_items),
            "action_items_total": len(self.action_items),
            "has_postmortem": self.postmortem is not None,
        }

    def to_dict(self) -> dict[str, Any]:
        out = self.summary()
        out.update({
            "declared_by": self.declared_by, "last_update_at": self.last_update_at,
            "updates": list(self.updates), "reminders": list(self.reminders),
            "timeline": [e.to_dict() for e in self.sorted_timeline()],
            "action_items": [a.to_dict() for a in self.action_items],
            "pages": list(self.pages), "status_incident_id": self.status_incident_id,
            "runbook_runs": list(self.runbook_runs),
        })
        return out
