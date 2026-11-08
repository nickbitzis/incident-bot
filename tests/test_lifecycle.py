import pytest

from incidentbot.policy import POLICIES


def test_declare_opens_channel_and_assigns_commander(h):
    inc = h.declare()
    ch = h.bot.store.get_channel(inc.channel_id)
    assert ch.name == "inc-1001-checkout-errors" and ch.incident_id == "INC-1001"
    assert inc.roles == {"commander": "hiro", "comms": None, "ops": None}
    assert inc.state == "investigating"
    announce = h.bot.store.messages(h.bot.incidents_channel.id)[-1].text
    assert "INC-1001 sev2 declared" in announce


def test_declare_ic_option_and_service_guess(h):
    h.bot.handle(__import__("incidentbot.chat", fromlist=["SlashCommand"]).SlashCommand(
        "ana", h.bot.incidents_channel.id, "/incident", 'declare sev3 "search is slow" ic=@ben'))
    inc = h.bot.incidents["INC-1001"]
    assert inc.service == "search" and inc.roles["commander"] == "ben"


@pytest.mark.parametrize("text,err", [
    ('declare sev9 "x" service=checkout', "severity must be"),
    ("declare sev2", "add a title"),
    ('declare sev2 "mystery" ', "add service="),
])
def test_declare_validation(h, text, err):
    h.channel = h.bot.incidents_channel.id
    h.cmd("ana", text)
    assert err in h.last_text()
    assert h.bot.incidents == {}


def test_full_lifecycle(staffed):
    h = staffed
    for state in ("identified", "monitoring"):
        h.clock.advance(minutes=5)
        h.cmd("hiro", f"state {state}")
    h.clock.advance(minutes=20)
    h.cmd("hiro", "resolve")
    inc = h.inc
    assert inc.state == "resolved"
    assert inc.identified_at < inc.mitigated_at < inc.resolved_at
    assert inc.postmortem is not None
    assert "Postmortem draft ready" in h.bot.store.messages(h.channel, 2)[-1].text


@pytest.mark.parametrize("path,bad", [
    ([], "resolved"),  # investigating -> resolved needs a cause or mitigation first
    (["identified"], "identified"),
    (["identified", "monitoring", "resolved"], "monitoring"),  # resolved is final
])
def test_illegal_transitions_rejected(staffed, path, bad):
    for s in path:
        staffed.cmd("hiro", f"state {s}")
    before = staffed.inc.state
    staffed.cmd("hiro", f"state {bad}")
    assert staffed.inc.state == before
    assert staffed.last_text().startswith("Not done")


def test_regression_back_to_investigating_keeps_first_mitigation(staffed):
    h = staffed
    h.cmd("hiro", "state monitoring")
    first = h.inc.mitigated_at
    h.clock.advance(minutes=10)
    h.cmd("hiro", "state investigating")
    h.clock.advance(minutes=10)
    h.cmd("hiro", "state monitoring")
    assert h.inc.mitigated_at == first


def test_role_taken_requires_handoff(staffed):
    staffed.cmd("ana", "role comms @ana")
    assert "Use /incident handoff comms" in staffed.last_text()
    assert staffed.inc.roles["comms"] == "grace"


def test_handoff_by_holder_records_previous(staffed):
    staffed.cmd("grace", "handoff comms @ana")
    assert staffed.inc.roles["comms"] == "ana"
    e = staffed.inc.timeline[-1]
    assert e.kind == "role" and e.data["previous"] == "grace" and e.data["user"] == "ana"


def test_handoff_by_commander_allowed_by_others_refused(staffed):
    staffed.cmd("ben", "handoff ops @ana")
    assert staffed.inc.roles["ops"] == "kofi"
    staffed.cmd("hiro", "handoff ops @ana")
    assert staffed.inc.roles["ops"] == "ana"


def test_commander_handoff(staffed):
    staffed.cmd("hiro", "handoff ic @lena")
    assert staffed.inc.roles["commander"] == "lena"
    staffed.cmd("hiro", "handoff ops @ana")  # hiro is no longer the commander
    assert staffed.inc.roles["ops"] == "kofi"


def test_claim_role_button(h):
    h.declare()
    h.press("grace", "claim_role", "comms")
    assert h.inc.roles["comms"] == "grace"


def test_sev2_pages_oncall_but_not_leadership(h):
    inc = h.declare("sev2")
    assert {p["target"] for p in inc.pages} == set(POLICIES["sev2"].page)
    assert inc.status_incident_id is None


def test_sev1_pages_leadership_and_opens_status_page(h):
    inc = h.declare("sev1")
    targets = {p["target"] for p in inc.pages}
    assert "eng-leadership" in targets and "exec-oncall" in targets
    assert inc.status_incident_id is not None
    assert len(h.bot.status.pending(inc.id)) == 1


def test_escalation_pages_only_new_targets(staffed):
    staffed.cmd("hiro", 'sev sev1 "all regions"')
    inc = staffed.inc
    targets = [p["target"] for p in inc.pages]
    assert len(targets) == len(set(targets))
    assert "eng-leadership" in targets
    assert inc.status_incident_id is not None
    assert inc.timeline[-1].kind in ("status", "page")


def test_downgrade_does_not_page(staffed):
    n = len(staffed.inc.pages)
    staffed.cmd("hiro", "sev sev3")
    assert staffed.inc.severity == "sev3" and len(staffed.inc.pages) == n


def test_commands_outside_incident_channel_rejected(h):
    h.channel = h.bot.incidents_channel.id
    h.cmd("ana", "state identified")
    assert "incident channel" in h.last_text()


def test_unknown_slash_command(h):
    from incidentbot.chat import SlashCommand

    out = h.bot.handle(SlashCommand("ana", h.bot.incidents_channel.id, "/deploy", "now"))
    assert "Unknown command /deploy" in out[0].text
