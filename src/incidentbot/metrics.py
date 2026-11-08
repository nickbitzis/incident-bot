"""Incident metrics: time to acknowledge, mitigate, and resolve, update cadence adherence,
counts by severity and service, and action item completion.

Definitions (all measured from detection, the first alert or the declaration if no alert):
- time to acknowledge: detection to declaration
- time to mitigate: detection to the first "monitoring" state (or resolution)
- time to resolve: detection to "resolved"
- cadence adherence: share of gaps between updates, from declaration to resolution, that were
  no longer than the severity's cadence. The declaration counts as the first update.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from incidentbot.models import SEVERITIES, Incident
from incidentbot.policy import policy_for

WEEK = 7 * 86400


def _min(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else (b - a) / 60


def durations(inc: Incident) -> dict[str, float | None]:
    return {
        "tta": _min(inc.detected_at, inc.declared_at),
        "ttm": _min(inc.detected_at, inc.mitigated_at),
        "ttr": _min(inc.detected_at, inc.resolved_at),
    }


def cadence_gaps(inc: Incident) -> tuple[int, int]:
    """(on_time, total) gaps for one incident. Open incidents count gaps up to the last update.
    Severities without a cadence return (0, 0)."""
    cadence = policy_for(inc.severity).cadence_minutes
    if cadence is None:
        return 0, 0
    end = inc.resolved_at if inc.resolved_at is not None else float("inf")
    points = sorted([inc.declared_at] + [u for u in inc.updates
                                         if inc.declared_at <= u < end])
    if inc.resolved_at is not None:
        points.append(inc.resolved_at)
    gaps = [b - a for a, b in zip(points, points[1:], strict=False)]
    on_time = sum(g <= cadence * 60 + 1e-6 for g in gaps)
    return on_time, len(gaps)


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p90": None}
    s = sorted(values)
    p90 = s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]
    return {"n": len(s), "mean": round(statistics.fmean(s), 1),
            "median": round(statistics.median(s), 1), "p90": round(p90, 1)}


def _block(group: list[Incident]) -> dict[str, Any]:
    ds = [durations(i) for i in group]
    gaps = [cadence_gaps(i) for i in group]
    on = sum(g[0] for g in gaps)
    total = sum(g[1] for g in gaps)
    return {
        "count": len(group),
        "tta": _stats([d["tta"] for d in ds if d["tta"] is not None]),
        "ttm": _stats([d["ttm"] for d in ds if d["ttm"] is not None]),
        "ttr": _stats([d["ttr"] for d in ds if d["ttr"] is not None]),
        "cadence_on_time": on,
        "cadence_gaps": total,
        "cadence_adherence": round(on / total, 3) if total else None,
        "reminders": sum(len(i.reminders) for i in group),
    }


def summarize(incidents: Iterable[Incident]) -> dict[str, Any]:
    incs = sorted(incidents, key=lambda i: i.detected_at)
    by_sev: dict[str, list[Incident]] = defaultdict(list)
    for i in incs:
        by_sev[i.severity].append(i)
    items = [a for i in incs for a in i.action_items]
    done = [a for a in items if a.done]
    weeks: list[dict[str, Any]] = []
    if incs:
        start = incs[0].detected_at
        weekly: Counter[int] = Counter(int((i.detected_at - start) // WEEK) for i in incs)
        weeks = [{"week": w + 1, "start": start + w * WEEK, "count": weekly.get(w, 0)}
                 for w in range(max(weekly) + 1)]
    return {
        "total": len(incs),
        "open": sum(i.is_open for i in incs),
        "overall": _block(incs),
        "by_severity": {s: _block(by_sev[s]) for s in SEVERITIES if by_sev.get(s)},
        "by_service": dict(Counter(i.service for i in incs).most_common()),
        "action_items": {
            "total": len(items),
            "done": len(done),
            "completion": round(len(done) / len(items), 3) if items else None,
            "median_days_to_close": round(statistics.median(
                [(a.done_at - a.created_at) / 86400 for a in done if a.done_at]), 1)
            if done else None,
        },
        "weekly": weeks,
        "incidents": [{**i.summary(),
                       **{k: (round(v, 1) if v is not None else None)
                          for k, v in durations(i).items()},
                       "cadence": list(cadence_gaps(i))} for i in incs],
    }
