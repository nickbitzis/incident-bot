import pytest

from incidentbot.chat import InMemoryChatStore, SqliteChatStore, actions, button, section


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryChatStore()
    return SqliteChatStore(str(tmp_path / "chat.db"))


def test_channels_create_and_lookup(store):
    c = store.create_channel("inc-1", 10.0, "topic", "INC-1")
    assert store.get_channel(c.id).incident_id == "INC-1"
    assert store.channel_by_name("inc-1").id == c.id
    assert [x.name for x in store.list_channels()] == ["inc-1"]


def test_duplicate_channel_name_rejected(store):
    store.create_channel("dup", 1.0)
    with pytest.raises(ValueError):
        store.create_channel("dup", 2.0)


def test_post_and_list_in_order_with_limit(store):
    c = store.create_channel("c", 1.0)
    for i in range(5):
        store.post(c.id, "ana", f"m{i}", float(i))
    assert [m.text for m in store.messages(c.id)] == ["m0", "m1", "m2", "m3", "m4"]
    assert [m.text for m in store.messages(c.id, limit=2)] == ["m3", "m4"]


def test_blocks_threads_and_bot_flag_round_trip(store):
    c = store.create_channel("c", 1.0)
    blocks = [section("hi"), actions(button("Go", "go", "1", "primary"))]
    parent = store.post(c.id, "ana", "parent", 1.0)
    m = store.post(c.id, "incidentbot", "reply", 2.0, thread_ts=parent.id, blocks=blocks,
                   bot=True)
    got = store.get_message(m.id)
    assert got.blocks == blocks and got.thread_ts == parent.id and got.bot


def test_reactions_are_deduplicated_per_user(store):
    c = store.create_channel("c", 1.0)
    m = store.post(c.id, "ana", "x", 1.0)
    assert store.add_reaction(m.id, "star", "ben") is True
    assert store.add_reaction(m.id, "star", "ben") is False
    store.add_reaction(m.id, "star", "kofi")
    assert store.get_message(m.id).reactions == {"star": ["ben", "kofi"]}


def test_update_blocks(store):
    c = store.create_channel("c", 1.0)
    m = store.post(c.id, "bot", "x", 1.0, blocks=[section("a")])
    store.update_blocks(m.id, [section("b")])
    assert store.get_message(m.id).blocks[0]["text"]["text"] == "b"


def test_post_to_missing_channel_fails(store):
    with pytest.raises(KeyError):
        store.post("C999", "ana", "x", 1.0)


def test_sqlite_persists_across_connections(tmp_path):
    path = str(tmp_path / "chat.db")
    s1 = SqliteChatStore(path)
    c = s1.create_channel("keep", 1.0)
    s1.post(c.id, "ana", "hello", 1.0)
    s2 = SqliteChatStore(path)
    assert [m.text for m in s2.messages(c.id)] == ["hello"]
