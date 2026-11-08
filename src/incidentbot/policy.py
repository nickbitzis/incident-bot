"""Severity policy: who gets paged, how often the channel must hear an update, status page."""

from __future__ import annotations

from dataclasses import dataclass

from incidentbot.models import SEVERITIES


@dataclass(frozen=True)
class SeverityPolicy:
    severity: str
    description: str
    cadence_minutes: int | None  # None means no update reminders
    page: tuple[str, ...]
    status_page: bool  # open a public status page incident on declare or escalation
    public_status: str  # component status while the incident is investigating or identified


POLICIES: dict[str, SeverityPolicy] = {
    "sev1": SeverityPolicy("sev1", "Customer-facing outage or data at risk", 15,
                           ("oncall-primary", "oncall-secondary", "eng-leadership",
                            "exec-oncall"), True, "major_outage"),
    "sev2": SeverityPolicy("sev2", "A major feature is degraded for many customers", 30,
                           ("oncall-primary", "eng-manager-oncall"), False, "partial_outage"),
    "sev3": SeverityPolicy("sev3", "A minor feature is degraded or a workaround exists", 60,
                           ("oncall-primary",), False, "degraded_performance"),
    "sev4": SeverityPolicy("sev4", "No customer impact, tracked for follow up", None, (),
                           False, "operational"),
}


def policy_for(severity: str) -> SeverityPolicy:
    if severity not in POLICIES:
        raise ValueError(f"unknown severity {severity!r}, use one of {', '.join(SEVERITIES)}")
    return POLICIES[severity]


def rank(severity: str) -> int:
    """sev1 is the most severe. Lower rank means more severe."""
    return SEVERITIES.index(severity)
