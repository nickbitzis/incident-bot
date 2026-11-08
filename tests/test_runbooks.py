import pytest

from incidentbot.runbooks import RunbookError, RunbookExecutor, load_runbooks, parse_runbook
from incidentbot.systems import FakeSystems


@pytest.fixture
def ex():
    s = FakeSystems.default()
    s.versions["checkout"] = ["v1", "v2", "v3"]
    return RunbookExecutor(s)


def test_shipped_runbooks_load():
    books = load_runbooks()
    assert set(books) == {"rollback-deploy", "scale-service", "flush-cache", "toggle-flag"}
    assert any(s.confirm for rb in books.values() for s in rb.steps)


def test_runbook_schema_errors():
    with pytest.raises(RunbookError, match="missing 'steps'"):
        parse_runbook({"name": "x", "title": "x"})
    with pytest.raises(RunbookError, match="duplicate step id"):
        parse_runbook({"name": "x", "title": "x", "steps": [
            {"id": "a", "action": "health.check"}, {"id": "a", "action": "health.check"}]})


def test_missing_required_param(ex):
    with pytest.raises(RunbookError, match="needs service"):
        ex.start("rollback-deploy", {}, "INC-1", "kofi", 0.0)


def test_unknown_param_rejected(ex):
    with pytest.raises(RunbookError, match="unknown parameter"):
        ex.start("flush-cache", {"service": "checkout", "region": "eu"}, "INC-1", "kofi", 0.0)


def test_rollback_waits_for_confirmation_then_applies(ex):
    run, reused = ex.start("rollback-deploy", {"service": "checkout"}, "INC-1", "kofi", 1.0)
    assert not reused and run.status == "awaiting_confirmation"
    assert [s.status for s in run.steps] == ["done", "awaiting_confirmation", "pending"]
    assert ex.systems.versions["checkout"][-1] == "v3"  # nothing changed yet
    run, acted = ex.confirm(run.id, "kofi", 2.0)
    assert acted and run.status == "completed"
    assert ex.systems.versions["checkout"][-1] == "v2"
    assert run.steps[1].by == "kofi"


def test_double_confirm_is_a_no_op(ex):
    run, _ = ex.start("rollback-deploy", {"service": "checkout"}, "INC-1", "kofi", 1.0)
    ex.confirm(run.id, "kofi", 2.0)
    _, acted = ex.confirm(run.id, "hiro", 3.0)
    assert not acted
    assert ex.systems.versions["checkout"] == ["v1", "v2", "v3", "v2"]


def test_same_run_is_not_repeated_and_force_reruns(ex):
    run, _ = ex.start("rollback-deploy", {"service": "checkout"}, "INC-1", "kofi", 1.0)
    ex.confirm(run.id, "kofi", 2.0)
    again, reused = ex.start("rollback-deploy", {"service": "checkout"}, "INC-1", "kofi", 3.0)
    assert reused and again.id == run.id
    assert [a.outcome for a in ex.audit][-1] == "reused"
    forced, reused = ex.start("rollback-deploy", {"service": "checkout"}, "INC-1", "kofi", 4.0,
                              force=True)
    assert not reused and forced.id != run.id


def test_target_is_pinned_so_a_rerun_does_not_roll_back_twice(ex):
    run, _ = ex.start("rollback-deploy", {"service": "checkout", "to": "v2"}, "INC-1", "k", 1.0)
    ex.confirm(run.id, "k", 2.0)
    forced, _ = ex.start("rollback-deploy", {"service": "checkout", "to": "v2"}, "INC-1", "k",
                         3.0, force=True)
    assert forced.status == "completed"  # nothing to confirm: already on v2
    assert forced.steps[1].result.startswith("no change")
    assert ex.systems.versions["checkout"][-1] == "v2"


def test_dry_run_changes_nothing_and_needs_no_confirmation(ex):
    before = list(ex.systems.versions["checkout"])
    run, _ = ex.start("rollback-deploy", {"service": "checkout"}, "INC-1", "kofi", 1.0,
                      dry_run=True)
    assert run.status == "dry_run"
    assert all(s.status == "planned" for s in run.steps)
    assert "would roll back checkout from v3 to v2" in run.steps[1].result
    assert ex.systems.versions["checkout"] == before
    assert {a.outcome for a in ex.audit} == {"planned"}


def test_dry_run_does_not_block_the_real_run(ex):
    ex.start("flush-cache", {"service": "auth"}, "INC-1", "kofi", 1.0, dry_run=True)
    run, reused = ex.start("flush-cache", {"service": "auth"}, "INC-1", "kofi", 2.0)
    assert not reused and run.status == "awaiting_confirmation"


def test_scale_and_flag_are_idempotent(ex):
    run, _ = ex.start("scale-service", {"service": "search", "replicas": "12"}, "I", "k", 1.0)
    ex.confirm(run.id, "k", 2.0)
    assert ex.systems.replicas["search"] == 12
    again, _ = ex.start("scale-service", {"service": "search", "replicas": "12"}, "I", "k", 3.0,
                        force=True)
    assert again.status == "completed" and not again.steps[1].changed
    run, _ = ex.start("toggle-flag", {"flag": "search-v2-ranking"}, "I", "k", 4.0)
    assert run.status == "completed"  # flag is already off, nothing to confirm


def test_failure_is_recorded(ex):
    run, _ = ex.start("rollback-deploy", {"service": "checkout", "to": "v0"}, "I", "k", 1.0)
    assert run.status == "failed" and "never ran v0" in run.error
    assert ex.audit[-1].outcome == "failed"


def test_cancel(ex):
    run, _ = ex.start("flush-cache", {"service": "auth"}, "I", "k", 1.0)
    run, acted = ex.cancel(run.id, "hiro", 2.0)
    assert acted and run.status == "cancelled"
    assert ex.systems.cache_keys["auth"] > 0


def test_chat_runbook_confirm_requires_ops_or_commander(staffed):
    h = staffed
    h.bot.systems.versions["checkout"] = ["v1", "v2"]
    h.cmd("kofi", "runbook rollback-deploy")
    run_id = h.inc.runbook_runs[-1]
    h.press("ben", "runbook_confirm", run_id)
    assert "only the ops lead" in h.last_text()
    h.press("hiro", "runbook_confirm", run_id)
    assert h.bot.executor.runs[run_id].status == "completed"
    applied = [e for e in h.inc.timeline if e.kind == "runbook" and "applied" in e.text]
    assert applied and applied[0].actor == "hiro"
    msg = h.bot.store.get_message(h.bot._run_msgs[run_id])
    assert all(b["type"] != "actions" for b in msg.blocks)  # buttons removed once done


def test_chat_rerun_reports_reuse(staffed):
    h = staffed
    h.cmd("kofi", "runbook flush-cache")
    h.press("kofi", "runbook_confirm", h.inc.runbook_runs[-1])
    h.cmd("kofi", "runbook flush-cache")
    assert "Nothing was repeated" in h.last_text()
