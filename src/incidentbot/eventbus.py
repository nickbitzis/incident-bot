"""A fake event bus for deploys, monitoring alerts, and feature flag changes.

In production this is a Kafka topic or an SNS fan-out from the CD system and the alert
manager. Here it is an in-process pub/sub that keeps a short history, so a newly declared
incident can pull in the deploy that happened three minutes before anyone noticed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

KINDS = ("deploy", "alert", "alert_resolved", "flag_change")


@dataclass
class BusEvent:
    kind: str
    ts: float
    service: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    def __init__(self, history: int = 5000) -> None:
        self._subs: list[Callable[[BusEvent], None]] = []
        self.history: list[BusEvent] = []
        self._limit = history
        self._seq = 0

    def subscribe(self, fn: Callable[[BusEvent], None]) -> None:
        self._subs.append(fn)

    def publish(self, event: BusEvent) -> BusEvent:
        if event.kind not in KINDS:
            raise ValueError(f"unknown bus event kind {event.kind!r}")
        self._seq += 1
        event.id = f"E{self._seq}"
        self.history.append(event)
        del self.history[:-self._limit]
        for fn in self._subs:
            fn(event)
        return event

    def recent(self, service: str, since: float, until: float) -> list[BusEvent]:
        return [e for e in self.history if e.service == service and since <= e.ts <= until]


def deploy(ts: float, service: str, version: str, author: str = "ci") -> BusEvent:
    return BusEvent("deploy", ts, service, f"deploy {version} to {service} by {author}",
                    {"version": version, "author": author})


def alert(ts: float, service: str, metric: str, value: float, threshold: float,
          affected_users: int = 0) -> BusEvent:
    unit = "%" if metric == "error_rate" else " ms"
    shown = round(value * 100, 1) if metric == "error_rate" else round(value)
    limit = round(threshold * 100, 1) if metric == "error_rate" else round(threshold)
    return BusEvent("alert", ts, service,
                    f"{metric.replace('_', ' ')} alert on {service}: {shown}{unit} "
                    f"(threshold {limit}{unit})",
                    {"metric": metric, "value": value, "threshold": threshold,
                     "affected_users": affected_users})


def alert_resolved(ts: float, service: str, metric: str) -> BusEvent:
    return BusEvent("alert_resolved", ts, service,
                    f"{metric.replace('_', ' ')} alert on {service} recovered", {"metric": metric})


def flag_change(ts: float, service: str, flag: str, enabled: bool, author: str) -> BusEvent:
    state = "on" if enabled else "off"
    return BusEvent("flag_change", ts, service, f"flag {flag} turned {state} by {author}",
                    {"flag": flag, "enabled": enabled, "author": author})
