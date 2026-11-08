from incidentbot.metrics import summarize
from incidentbot.simulation import QUARTER_END, QUARTER_START, check, run_quarter


def test_quarter_resolves_all_incidents_with_complete_timelines():
    res = run_quarter(seed=7)
    assert len(res.incidents) == 20
    assert check(res.incidents) == []
    assert all(QUARTER_START <= i.detected_at <= QUARTER_END for i in res.incidents)


def test_quarter_is_reproducible_and_seed_sensitive():
    a = summarize(run_quarter(seed=7).incidents)
    b = summarize(run_quarter(seed=7).incidents)
    c = summarize(run_quarter(seed=8).incidents)
    assert a == b
    assert a["incidents"] != c["incidents"]


def test_quarter_exercises_every_feature():
    res = run_quarter(seed=7)
    kinds = {e.kind for i in res.incidents for e in i.timeline}
    assert {"deploy", "alert", "flag_change", "runbook", "message", "action", "status",
            "reminder", "page", "severity"} <= kinds
    s = summarize(res.incidents)
    assert set(s["by_severity"]) >= {"sev1", "sev2", "sev3"}
    assert 0.5 < s["overall"]["cadence_adherence"] < 1.0
    assert 0 < s["action_items"]["done"] < s["action_items"]["total"]
    assert any(e.data.get("previous") for i in res.incidents for e in i.timeline
               if e.kind == "role")


def test_check_flags_unresolved_incident():
    res = run_quarter(seed=7)
    inc = res.incidents[0]
    inc.state = "monitoring"
    inc.postmortem = None
    problems = check(res.incidents)
    assert any("not resolved" in p for p in problems)
    assert any("no postmortem" in p for p in problems)
