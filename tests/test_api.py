import pytest
from fastapi.testclient import TestClient

from incidentbot.api import build_demo, create_app


@pytest.fixture(scope="module")
def client():
    app = create_app(bot=build_demo(), tick_seconds=3600)
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def live(client):
    return client.get("/api/incidents", params={"state": "identified"}).json()["incidents"][0]


def test_healthz_and_prometheus(client):
    assert client.get("/healthz").json()["status"] == "ok"
    text = client.get("/metrics").text
    assert "incidentbot_open_incidents 1" in text
    assert 'incidentbot_incidents{severity="sev1",state="resolved"}' in text
    assert "incidentbot_commands_total" in text


def test_demo_has_quarter_and_live_incident(client, live):
    rows = client.get("/api/incidents").json()["incidents"]
    assert len(rows) == 21
    assert live["title"] == "checkout errors" and live["roles"]["ops"] == "kofi"


def test_incident_detail_includes_runs_audit_and_timeline(client, live):
    d = client.get(f"/api/incidents/{live['id']}").json()
    assert d["runbook_runs"][-1]["status"] == "awaiting_confirmation"
    assert any(a["outcome"] == "planned" for a in d["audit"])
    kinds = [e["kind"] for e in d["timeline"]]
    assert kinds[:3] == ["deploy", "alert", "declare"]


def test_chat_flow_end_to_end(client, live):
    ch = live["channel_id"]
    run_id = client.get(f"/api/incidents/{live['id']}").json()["runbook_runs"][-1]["id"]
    r = client.post("/api/chat/interaction", json={"user": "kofi", "channel_id": ch,
                                                   "action_id": "runbook_confirm",
                                                   "value": run_id})
    assert r.status_code == 200 and "confirmed" in r.json()["messages"][-1]["text"]
    pending = client.get("/api/status/internal").json()["updates"]
    su = [u for u in pending if u["status"] == "pending_approval"][-1]["id"]
    client.post("/api/chat/interaction", json={"user": "grace", "channel_id": ch,
                                               "action_id": "status_approve", "value": su})
    assert client.get("/api/status").json()["overall"] != "operational"
    client.post("/api/chat/message", json={"user": "hiro", "channel_id": ch,
                                           "text": "/incident state monitoring"})
    r = client.post("/api/chat/command", json={"user": "hiro", "channel_id": ch,
                                               "text": "/incident resolve"})
    assert "Postmortem draft ready" in r.json()["messages"][-1]["text"]
    md = client.get(f"/api/incidents/{live['id']}/postmortem",
                    params={"format": "markdown"}).text
    assert md.startswith(f"# Postmortem: {live['id']}") and "DRAFT" in md


def test_star_via_reaction_endpoint(client):
    ch = client.get("/api/channels").json()["channels"]
    inc_ch = [c for c in ch if c["incident_id"]][0]["id"]
    m = client.post("/api/chat/message", json={"user": "ana", "channel_id": inc_ch,
                                               "text": "found it"}).json()
    msgs = client.get(f"/api/channels/{inc_ch}/messages").json()["messages"]
    r = client.post("/api/chat/reaction", json={"user": "ana", "channel_id": inc_ch,
                                                "message_id": msgs[-1]["id"]})
    assert m["ok"] and "Added to the timeline" in r.json()["messages"][0]["text"]


def test_declare_from_ui_and_bus_alert(client):
    channels = client.get("/api/channels").json()["channels"]
    general = next(c for c in channels if c["name"] == "incidents")["id"]
    client.post("/api/bus/alert", json={"service": "search", "metric": "p99_latency",
                                        "value": 3000, "threshold": 1200})
    client.post("/api/chat/message", json={"user": "ana", "channel_id": general,
                                           "text": '/incident declare sev3 "search slow" '
                                                   "service=search"})
    rows = client.get("/api/incidents", params={"service": "search", "state": "investigating"})
    inc = rows.json()["incidents"][0]
    d = client.get(f"/api/incidents/{inc['id']}").json()
    assert d["timeline"][0]["kind"] == "alert"


def test_slack_events_endpoint(client):
    assert client.post("/slack/events", json={"type": "url_verification",
                                              "challenge": "abc"}).json() == {"challenge": "abc"}
    general = next(c for c in client.get("/api/channels").json()["channels"]
                   if c["name"] == "incidents")["id"]
    body = f"command=%2Fincident&text=help&user_id=ana&channel_id={general}"
    r = client.post("/slack/events", content=body,
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert "Incident commands" in r.json()["messages"][0]["text"]
    assert client.post("/slack/events", json={"type": "nope"}).status_code == 400


def test_metrics_summary_and_runbooks(client):
    s = client.get("/api/metrics/summary").json()
    assert s["total"] >= 21 and "sev1" in s["by_severity"]
    rb = client.get("/api/runbooks").json()
    assert len(rb["runbooks"]) == 4


def test_errors(client):
    assert client.get("/api/incidents/INC-9999").status_code == 404
    assert client.post("/api/chat/message", json={"user": "a", "channel_id": "C404",
                                                  "text": "x"}).status_code == 404
    assert client.post("/api/bus/deploy", json={"service": "mainframe",
                                                "version": "v1"}).status_code == 422
    open_inc = client.get("/api/incidents", params={"state": "investigating"}).json()
    iid = open_inc["incidents"][0]["id"]
    assert client.get(f"/api/incidents/{iid}/postmortem").status_code == 409


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "incidentbot" in r.text
    assert client.get("/static/app.js").status_code == 200
