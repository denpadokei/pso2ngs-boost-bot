from datetime import datetime

from cloud.storage import (
    GUILD_PARTITION,
    GuildConfig,
    TableStore,
    guild_to_row,
    row_to_guild,
    rows_to_state,
    state_to_rows,
)
from fakes import FakeTableService
from pso2boost.models import BoostEvent, event_key
from pso2boost.periods import parse_period
from pso2boost.state import State
from pso2boost.tz import JST

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=JST)


def make_event(label: str, period_text: str) -> BoostEvent:
    period = parse_period(period_text, 2026)
    return BoostEvent(key=event_key(label, period), label=label, period=period)


def make_store() -> tuple[TableStore, FakeTableService]:
    service = FakeTableService()
    store = TableStore("unused", service=service)
    store.ensure_tables()
    return store, service


# ---------------------------------------------------------------- 変換（純粋関数）
def test_state_to_rows_and_back_roundtrip():
    state = State(None, NOW)
    event = make_event("ブーストキャンペーン 第2弾", "11/25 12:00～12/2 メンテナンス開始まで")
    state.mark_seen(event)
    state.mark_sent("new", event.key)
    state.mark_sent("start", event.key)
    state.set_discord_event(
        event.key, {"id": "ev7", "signature": "sig", "name": event.label, "end": "2026-12-02T11:00:00+09:00"}
    )
    state.mark_rss("https://pso2.jp/players/news/cgcNov26/")

    rows = state_to_rows("111", state)
    restored = rows_to_state(rows, NOW)

    assert restored.is_seen(event.key)
    assert restored.was_sent("new", event.key)
    assert restored.was_sent("start", event.key)
    assert not restored.was_sent("end", event.key)
    entry = restored.discord_event(event.key)
    assert entry["id"] == "ev7"
    assert entry["signature"] == "sig"
    assert entry["end"] == "2026-12-02T11:00:00+09:00"
    assert restored.rss_seen("https://pso2.jp/players/news/cgcNov26/")


def test_rowkeys_are_table_safe():
    url = "https://pso2.jp/players/news/cgcNov26/#cp3"
    state = State(None, NOW)
    state.mark_rss(url)

    rows = state_to_rows("111", state)

    rss_row = next(r for r in rows if str(r["RowKey"]).startswith("rss:"))
    row_key = str(rss_row["RowKey"])
    assert not any(ch in row_key for ch in "/?#\\")
    restored = rows_to_state(rows, NOW)
    assert restored.rss_seen(url)


def test_seen_only_state_restores_first_seen_fallback():
    """seen が無くても new_sent があれば「告知済み」として復元する。"""
    state = State(None, NOW)
    event = make_event("ブーストキャンペーン 第1弾", "11/11 12:00～11/18 メンテナンス開始まで")
    state.mark_seen(event)
    state.mark_sent("new", event.key)

    rows = state_to_rows("111", state)
    for row in rows:
        row.pop("first_seen", None)
        row.pop("last_seen", None)

    restored = rows_to_state(rows, NOW)
    assert restored.is_seen(event.key)
    assert restored.was_sent("new", event.key)


def test_guild_row_roundtrip():
    guild = GuildConfig(
        guild_id="123",
        channel_id="456",
        enabled=False,
        permissions_ok=False,
        added_at="2026-11-01T00:00:00+09:00",
        last_run_at="2026-11-18T12:00:00+09:00",
        last_error="HTTP 403: Forbidden",
    )
    row = guild_to_row(guild)
    assert row["PartitionKey"] == GUILD_PARTITION
    assert row["RowKey"] == "123"
    assert len(row["last_error"]) <= 200

    restored = row_to_guild(row)
    assert restored == guild


# ---------------------------------------------------------------- TableStore
def test_upsert_and_list_guilds():
    store, _ = make_store()
    store.upsert_guild(GuildConfig(guild_id="123", channel_id="c1"))
    store.upsert_guild(GuildConfig(guild_id="456", channel_id="c2", enabled=False))

    guilds = {g.guild_id: g for g in store.list_guilds()}
    assert set(guilds) == {"123", "456"}
    assert guilds["123"].channel_id == "c1"
    assert guilds["456"].enabled is False


def test_save_and_load_state():
    store, _ = make_store()
    state = State(None, NOW)
    event = make_event("ブーストキャンペーン 第2弾", "11/25 12:00～12/2 メンテナンス開始まで")
    state.mark_seen(event)
    state.mark_sent("new", event.key)

    store.save_state("123", state)
    loaded = store.load_state("123", NOW)

    assert loaded.is_seen(event.key)
    assert loaded.was_sent("new", event.key)
    # 別パーティションは独立
    other = store.load_state("456", NOW)
    assert not other.is_seen(event.key)


def test_save_state_deletes_stale_rows():
    store, service = make_store()
    # 前回の残骸（今は state に無い行）を仕込む
    service.upsert_entity(
        store.state_table,
        {"PartitionKey": "123", "RowKey": "event:old", "end": "2026-10-01T11:00:00+09:00"},
    )

    state = State(None, NOW)
    store.save_state("123", state)

    rows = service.list_entities(store.state_table, query="PartitionKey eq '123'")
    assert rows == []
    assert ("state", "123", "event:old") in service.deleted


def test_record_guild_run_updates_registered_guild_only():
    store, service = make_store()
    store.upsert_guild(GuildConfig(guild_id="123", channel_id="c1"))

    store.record_guild_run("123", NOW, last_error="投稿に失敗")
    store.record_guild_run("999", NOW, last_error="登録されていない")

    guilds = {g.guild_id: g for g in store.list_guilds()}
    assert guilds["123"].last_error == "投稿に失敗"
    assert guilds["123"].last_run_at == NOW.isoformat()
    assert "999" not in guilds

    # 成功すれば last_error はクリアされる
    store.record_guild_run("123", NOW, last_error=None)
    assert {g.guild_id: g for g in store.list_guilds()}["123"].last_error == ""
