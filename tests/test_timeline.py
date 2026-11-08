from incidentbot.eventbus import alert, deploy, flag_change


def test_bus_events_before_declare_are_backfilled_in_order(h):
    t0 = h.clock.now()
    h.bot.bus.publish(deploy(t0, "checkout", "v2026.11.3"))
    h.clock.advance(minutes=3)
    h.bot.bus.publish(alert(h.clock.now(), "checkout", "error_rate", 0.12, 0.02, 4200))
    h.clock.advance(minutes=4)
    inc = h.declare()
    tl = inc.sorted_timeline()
    assert [e.kind for e in tl[:3]] == ["deploy", "alert", "declare"]
    assert tl[0].source == "bus" and "v2026.11.3" in tl[0].text
    assert inc.detected_at == t0 + 180
    assert inc.affected_users == 4200


def test_old_and_unrelated_bus_events_are_not_backfilled(h):
    h.bot.bus.publish(deploy(h.clock.now(), "checkout", "v1"))
    h.bot.bus.publish(deploy(h.clock.now(), "search", "v9"))
    h.clock.advance(minutes=120)
    inc = h.declare()
    assert "deploy" not in [e.kind for e in inc.timeline]


def test_live_bus_events_land_on_open_incident(staffed):
    staffed.clock.advance(minutes=2)
    staffed.bot.bus.publish(flag_change(staffed.clock.now(), "checkout", "new-checkout-flow",
                                        False, "ana"))
    e = staffed.inc.timeline[-1]
    assert e.kind == "flag_change" and e.source == "bus"


def test_unowned_alert_offers_declare_button(h):
    h.bot.bus.publish(alert(h.clock.now(), "search", "p99_latency", 3000, 1200, 50))
    m = h.bot.store.messages(h.bot.alerts_channel.id)[-1]
    btn = m.blocks[-1]["elements"][0]
    assert btn["action_id"] == "declare_from_alert" and btn["value"].startswith("sev2|search|")
    h.press("kofi", "declare_from_alert", btn["value"], channel=h.bot.alerts_channel.id)
    assert h.bot.incidents["INC-1001"].service == "search"


def test_commands_role_and_state_changes_are_entries(staffed):
    staffed.cmd("hiro", "state identified")
    staffed.cmd("hiro", 'note "db failover ruled out"')
    kinds = staffed.kinds()
    for k in ("declare", "role", "page", "state", "note"):
        assert k in kinds
    assert sum(k == "role" for k in kinds) == 3


def test_starred_message_uses_time_said_and_is_added_once(staffed):
    h = staffed
    said = h.clock.now()
    msgs = h.msg("kofi", "v2026.11.3 touched the tax code")
    mid = msgs[-1].id
    h.clock.advance(minutes=5)
    h.react("hiro", mid)
    h.react("grace", mid)
    starred = [e for e in h.inc.timeline if e.kind == "message"]
    assert len(starred) == 1 and starred[0].ts == said
    assert starred[0].data["starred_by"] == "hiro"


def test_other_reactions_do_not_star(staffed):
    mid = staffed.msg("kofi", "lol")[-1].id
    staffed.react("hiro", mid, "eyes")
    assert "message" not in staffed.kinds()


def test_action_tag_creates_item_with_owner(staffed):
    staffed.msg("ana", "#action add a canary stage for checkout @kofi")
    item = staffed.inc.action_items[-1]
    assert item.text == "add a canary stage for checkout" and item.owner == "kofi"
    assert staffed.inc.entry(item.source_entry_id).kind == "action"


def test_action_command_and_done(staffed):
    staffed.cmd("hiro", 'action "write the postmortem" owner=@hiro')
    staffed.cmd("hiro", "done a1")
    assert staffed.inc.action_items[0].done
    staffed.cmd("hiro", "done A1")
    assert "already done" in staffed.last_text()
