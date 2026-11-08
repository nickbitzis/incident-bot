import pytest

from incidentbot.statuspage import ApprovalError, StatusPage


def test_public_update_needs_comms_approval(staffed):
    h = staffed
    h.cmd("grace", "status")
    [u] = h.bot.status.pending(h.inc.id)
    assert "investigating checkout errors affecting Payments and checkout" in u.body
    assert h.bot.status.public_view()["incidents"] == []
    h.press("hiro", "status_approve", u.id)  # the commander is not the comms lead
    assert "only @grace can approve" in h.last_text()
    h.press("grace", "status_approve", u.id)
    view = h.bot.status.public_view()
    assert view["incidents"][0]["updates"][0]["body"] == u.body
    comp = {c["name"]: c["status"] for c in view["components"]}
    assert comp["Payments and checkout"] == "partial_outage"


def test_approval_without_comms_lead_fails(h):
    h.declare("sev1")
    [u] = h.bot.status.pending(h.inc.id)
    h.cmd("hiro", f"approve {u.id}")
    assert "none assigned" in h.last_text()


def test_reject_and_double_approve(staffed):
    h = staffed
    h.cmd("grace", 'status "custom text"')
    [u] = h.bot.status.pending(h.inc.id)
    h.cmd("grace", f"reject {u.id} too vague")
    assert h.bot.status.updates[u.id].status == "rejected"
    h.cmd("grace", f"approve {u.id}")
    assert "already rejected" in h.last_text()


def test_resolution_drafts_resolved_update_and_clears_component(staffed):
    h = staffed
    h.cmd("grace", "status")
    h.press("grace", "status_approve", h.bot.status.pending(h.inc.id)[0].id)
    h.cmd("hiro", "state monitoring")
    h.cmd("hiro", "resolve")
    [u] = h.bot.status.pending(h.inc.id)
    assert u.state == "resolved"
    h.press("grace", "status_approve", u.id)
    assert h.bot.status.public_view()["overall"] == "operational"


def test_internal_updates_publish_immediately(staffed):
    staffed.cmd("kofi", 'update "rollback in progress"')
    internal = staffed.bot.status.internal_view()
    assert internal["updates"][-1]["visibility"] == "internal"
    assert internal["updates"][-1]["status"] == "published"
    assert internal["services"]["checkout"] == "partial_outage"


def test_monitoring_shows_degraded_and_worst_status_wins():
    sp = StatusPage()
    a = sp.open("INC-1", "a", "checkout", "major_outage", 0)
    b = sp.open("INC-2", "b", "payments-api", "partial_outage", 0)
    for si, state in ((a, "monitoring"), (b, "identified")):
        u = sp.draft_public(si, state, "x", "bot", 0)
        sp.approve(u.id, "grace", "grace", 1)
    assert sp.components["Payments and checkout"] == "partial_outage"
    with pytest.raises(ApprovalError):
        sp.approve("SU-999", "grace", "grace", 2)
