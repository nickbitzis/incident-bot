def test_no_reminder_before_cadence(h):
    h.declare("sev1")
    h.clock.advance(minutes=14)
    assert h.bot.tick() == []


def test_sev1_reminder_at_15_minutes_pings_comms(staffed):
    h = staffed
    h.cmd("hiro", "sev sev1")
    h.clock.advance(minutes=15)
    out = h.bot.tick()
    assert len(out) == 1 and "@grace" in out[0].text and "every 15 minutes" in out[0].text
    assert h.inc.timeline[-1].kind == "reminder"


def test_reminder_not_repeated_within_window(h):
    h.declare("sev2")
    h.clock.advance(minutes=30)
    assert len(h.bot.tick()) == 1
    h.clock.advance(minutes=10)
    assert h.bot.tick() == []
    h.clock.advance(minutes=20)
    assert len(h.bot.tick()) == 1  # still silent after another full window


def test_update_resets_the_clock(h):
    h.declare("sev2")
    h.clock.advance(minutes=25)
    h.cmd("hiro", 'update "rolling back"')
    h.clock.advance(minutes=25)
    assert h.bot.tick() == []
    h.clock.advance(minutes=5)
    assert len(h.bot.tick()) == 1


def test_sev4_has_no_cadence_and_resolved_incidents_are_quiet(staffed):
    h = staffed
    h.cmd("hiro", "sev sev4")
    h.clock.advance(minutes=600)
    assert h.bot.tick() == []
    h.cmd("hiro", "sev sev3")
    h.cmd("hiro", "state monitoring")
    h.cmd("hiro", "resolve")
    h.clock.advance(minutes=600)
    assert h.bot.tick() == []


def test_reminder_falls_back_to_commander_without_comms(h):
    h.declare("sev3")
    h.clock.advance(minutes=60)
    assert "@hiro" in h.bot.tick()[0].text
