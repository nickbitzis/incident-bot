import json
from types import SimpleNamespace

import pytest

from incidentbot.eventbus import alert, deploy
from incidentbot.postmortem import (
    NARRATIVE_SCHEMA,
    ClaudeWriter,
    TemplateWriter,
    build_facts,
    generate,
    to_markdown,
    validate_narrative,
)


class FakeClient:
    """Stands in for anthropic.Anthropic(): records the request, returns canned JSON."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=900, output_tokens=300))


@pytest.fixture
def resolved(h):
    t0 = h.clock.now()
    h.bot.bus.publish(deploy(t0, "checkout", "v2026.11.3"))
    h.clock.advance(minutes=3)
    h.bot.bus.publish(alert(h.clock.now(), "checkout", "error_rate", 0.12, 0.02, 4210))
    h.clock.advance(minutes=2)
    h.declare()
    h.cmd("hiro", "role comms @grace")
    h.cmd("hiro", "role ops @kofi")
    h.clock.advance(minutes=5)
    h.msg("ana", "#action add a canary stage @kofi")
    h.cmd("hiro", "state identified")
    h.cmd("kofi", "runbook rollback-deploy")
    h.press("kofi", "runbook_confirm", h.inc.runbook_runs[-1])
    h.clock.advance(minutes=10)
    h.cmd("hiro", "state monitoring")
    h.clock.advance(minutes=20)
    h.cmd("hiro", "resolve")
    return h


def narrative(ids, **over):
    base = {"summary": "Checkout failed after a deploy.", "summary_citations": ids[:2],
            "contributing_factors": [{"text": "Deploy shipped a tax bug.", "citations": ids[:1]}],
            "what_went_well": [], "what_went_poorly": [], "suggested_action_items": []}
    base.update(over)
    return base


def test_template_draft_structure_and_impact(resolved):
    pm = resolved.inc.postmortem
    assert pm["status"] == "draft" and "DRAFT" in pm["banner"]
    assert pm["generator"] == "template"
    assert pm["impact"]["customer_impact_minutes"] == 17  # alert to monitoring
    assert pm["impact"]["total_duration_minutes"] == 37
    assert pm["impact"]["affected_users"] == 4210
    assert pm["action_items"][0]["text"] == "add a canary stage"
    assert validate_narrative({k: pm[k] for k in NARRATIVE_SCHEMA["required"]},
                              {e.id for e in resolved.inc.timeline}) == []
    factor = pm["contributing_factors"][0]["text"]
    assert "deploy v2026.11.3" in factor and "3 minutes before the first alert" in factor


def test_markdown_has_sections_and_citations(resolved):
    md = to_markdown(resolved.inc.postmortem)
    for heading in ("## Summary", "## Impact", "## Timeline (UTC)", "## Contributing factors",
                    "## Action items"):
        assert heading in md
    assert "[T1, T2]" in md and "Affected users: 4,210" in md


def test_facts_skip_reminders_and_commands(resolved):
    facts = build_facts(resolved.inc)
    assert all(e["kind"] not in ("reminder", "command") for e in facts["timeline"])


def test_claude_writer_request_and_valid_citations(resolved):
    ids = [e.id for e in resolved.inc.sorted_timeline()]
    client = FakeClient(narrative(ids))
    pm = generate(resolved.inc, ClaudeWriter(client=client), 0.0)
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "T1" in call["messages"][0]["content"]
    assert pm["generator"] == "claude" and pm["citation_errors"] == []
    assert pm["summary"] == "Checkout failed after a deploy."
    assert pm["impact"]["affected_users"] == 4210  # numbers never come from the model


def test_unknown_citations_are_dropped_and_flagged(resolved):
    ids = [e.id for e in resolved.inc.timeline]
    bad = narrative(ids, contributing_factors=[{"text": "Made up.", "citations": ["T999"]}])
    pm = generate(resolved.inc, ClaudeWriter(client=FakeClient(bad)), 0.0)
    assert pm["citation_errors"] == ["contributing_factors[0] cites unknown entry T999"]
    assert pm["contributing_factors"][0] == {"text": "Made up.", "citations": [],
                                             "unverified": True}


def test_invalid_json_falls_back_to_template(resolved):
    pm = generate(resolved.inc, ClaudeWriter(client=FakeClient("not json")), 0.0)
    assert pm["generator"] == "template (fallback from claude)"
    assert pm["citation_errors"][0].startswith("writer failed")


def test_missing_fields_fall_back_to_template(resolved):
    pm = generate(resolved.inc, ClaudeWriter(client=FakeClient({"summary": "x"})), 0.0)
    assert pm["generator"].startswith("template")
    assert "missing summary_citations" in pm["citation_errors"]


def test_validate_rejects_non_object():
    assert validate_narrative([], set()) == ["narrative is not a JSON object"]


def test_template_writer_is_deterministic(resolved):
    facts = build_facts(resolved.inc)
    assert TemplateWriter().write(facts) == TemplateWriter().write(facts)
