import pytest

from incidentbot.metrics import cadence_gaps, durations, summarize
from incidentbot.models import ActionItem, Incident

M = 60.0


def make(sev="sev2", detected=0.0, declared=5 * M, mitigated=30 * M, resolved=60 * M,
         updates=(), service="checkout"):
    inc = Incident("INC-1", "t", sev, service, "C1", "hiro", declared, detected,
                   last_update_at=declared)
    inc.mitigated_at, inc.resolved_at = mitigated, resolved
    inc.state = "resolved" if resolved else "monitoring"
    inc.updates = list(updates)
    return inc


def test_durations_from_detection():
    assert durations(make()) == {"tta": 5.0, "ttm": 30.0, "ttr": 60.0}


def test_cadence_all_on_time():
    # sev2 cadence 30: declare 5, updates 30 and 55, resolve 60 -> gaps 25, 25, 5
    assert cadence_gaps(make(updates=[30 * M, 55 * M])) == (3, 3)


def test_cadence_late_gap_counted():
    # declare 5, update 40 (35 min gap, late), resolve 60
    assert cadence_gaps(make(updates=[40 * M])) == (1, 2)


def test_cadence_ignores_updates_after_resolution_and_sev4():
    assert cadence_gaps(make(updates=[20 * M, 70 * M])) == (1, 2)  # 15 on time, 40 late
    assert cadence_gaps(make(sev="sev4", updates=[20 * M])) == (0, 0)


def test_summary_by_severity_and_service():
    a = make("sev1", updates=[15 * M, 30 * M, 45 * M], service="checkout")
    b = make("sev2", detected=0, declared=10 * M, mitigated=50 * M, resolved=100 * M,
             updates=[60 * M], service="search")
    s = summarize([a, b])
    assert s["total"] == 2 and s["open"] == 0
    assert s["by_service"] == {"checkout": 1, "search": 1}
    assert s["by_severity"]["sev1"]["ttr"]["median"] == 60.0
    assert s["overall"]["tta"]["median"] == pytest.approx(7.5)
    # sev1: gaps 10,15,15,15 on time; sev2: gaps 50 (late), 40 (late)
    assert s["by_severity"]["sev1"]["cadence_adherence"] == 1.0
    assert s["by_severity"]["sev2"]["cadence_adherence"] == 0.0
    assert s["overall"]["cadence_adherence"] == pytest.approx(4 / 6, abs=1e-3)


def test_action_item_completion():
    inc = make()
    inc.action_items = [ActionItem("A1", "x", None, 0.0, "T1", True, 86400 * 2),
                        ActionItem("A2", "y", None, 0.0, "T1", True, 86400 * 4),
                        ActionItem("A3", "z", None, 0.0, "T1")]
    ai = summarize([inc])["action_items"]
    assert ai == {"total": 3, "done": 2, "completion": 0.667, "median_days_to_close": 3.0}


def test_open_incident_and_weekly_buckets():
    a = make()
    b = make(detected=8 * 86400, declared=8 * 86400 + 60, mitigated=None, resolved=None)
    s = summarize([a, b])
    assert s["open"] == 1
    assert [w["count"] for w in s["weekly"]] == [1, 1]
    assert s["overall"]["ttr"]["n"] == 1


def test_empty():
    s = summarize([])
    assert s["total"] == 0 and s["weekly"] == [] and s["overall"]["cadence_adherence"] is None
