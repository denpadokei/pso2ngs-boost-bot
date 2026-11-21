"""週次バッチ（cloud.runner）の結合テスト。ネットワークなし。

フィクスチャから作ったイベント一覧を、2つの「サーバー」が
別々のチャンネルへ受け取れることを確認する。
"""

from datetime import datetime
from pathlib import Path

from cloud.config import CloudConfig
from cloud.runner import resolve_targets, run_weekly
from cloud.storage import GuildConfig, TableStore
from fakes import FakeApi, FakeDiscordRest, FakeFetcher, FakeNotifier, FakeTableService
from pso2boost.fetch import SCHEDULE_URL
from pso2boost.pso2day import PSO2_DAY_URL
from pso2boost.rss import RSS_URL
from pso2boost.schedule import DEFAULT_KEYWORDS
from pso2boost.tz import JST

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=JST)
ARTICLE_URL = "https://pso2.jp/players/news/cgcNov26/"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_fetcher() -> FakeFetcher:
    return FakeFetcher(
        {
            SCHEDULE_URL: load("schedule.html"),
            RSS_URL: load("pso2news.xml"),
            ARTICLE_URL: load("article_32486.html"),
            PSO2_DAY_URL: load("article_31542.html"),
        }
    )


def make_config(**overrides) -> CloudConfig:
    values = dict(
        bot_token="test-token",
        storage_connection="unused",
        keywords=DEFAULT_KEYWORDS,
        concurrency=1,
    )
    values.update(overrides)
    return CloudConfig(**values)


def make_run(guild_specs: list[tuple[str, str, bool]], guild_status: dict[str, int] | None = None):
    """guild_specs: (guild_id, channel_id, notifier_fail) のリスト。"""
    service = FakeTableService()
    store = TableStore("unused", service=service)
    store.ensure_tables()
    apis: dict[str, FakeApi] = {}
    notifiers: dict[str, FakeNotifier] = {}
    for guild_id, channel_id, fail in guild_specs:
        store.upsert_guild(GuildConfig(guild_id=guild_id, channel_id=channel_id))
        apis[guild_id] = FakeApi(guild_id)
        notifiers[guild_id] = FakeNotifier(channel_id, fail=fail)

    guild_client = FakeDiscordRest(guild_status)

    def run(config=None, fetcher=None):
        return run_weekly(
            config or make_config(),
            now=NOW,
            store=store,
            fetcher=fetcher or make_fetcher(),
            guild_client=guild_client,
            api_factory=lambda target: apis[target.guild_id],
            notifier_factory=lambda target: notifiers[target.guild_id],
        )

    return store, apis, notifiers, run


def test_two_guilds_receive_notifications_in_separate_channels():
    store, apis, notifiers, run = make_run(
        [("111", "chan-A", False), ("222", "chan-B", False)]
    )

    summary = run()

    assert summary.guilds == 2
    assert summary.failed == 0

    # 両方のサーバーが同じ内容を、別々のチャンネルで受け取る
    kinds_a = sorted(kind for kind, _, _ in notifiers["111"].sent)
    kinds_b = sorted(kind for kind, _, _ in notifiers["222"].sent)
    assert kinds_a == kinds_b
    assert "new" in kinds_a and "end" in kinds_a

    # イベント作成もサーバーごとに行われる
    assert len(apis["111"].created) == len(apis["222"].created)
    assert apis["111"].created and apis["222"].created

    # state がサーバーごとに保存されている（片方だけ告知済みでも他方に影響しない）
    state_a = store.load_state("111", NOW)
    state_b = store.load_state("222", NOW)
    assert state_a.snapshot()["seen"]
    assert state_b.snapshot()["seen"] == state_a.snapshot()["seen"]

    # 次の週は同じ内容を送らない（冪等性）
    second = run()
    assert second.notified == 0
    assert second.failed == 0
    assert second.created == 0


def test_pso2_pages_are_fetched_once_for_two_guilds():
    fetcher = make_fetcher()
    _, _, _, run = make_run([("111", "chan-A", False), ("222", "chan-B", False)])

    run(fetcher=fetcher)

    # どの URL も1回しか取得していない（Fetcher のキャッシュ経由）
    for url in (SCHEDULE_URL, RSS_URL, ARTICLE_URL, PSO2_DAY_URL):
        assert fetcher.requested.count(url) == 1, url


def test_pso2_day_events_are_included_in_shared_bot():
    """共有Botでも PSO2の日のレア泥期間がイベント化・通知される。"""
    store, apis, notifiers, run = make_run([("111", "chan-A", False)])

    summary = run()

    assert summary.failed == 0
    names = [payload["name"] for payload in apis["111"].created]
    pso2day = [payload for payload in apis["111"].created if "PSO2の日" in payload["name"]]
    # 11/1～3 は期限切れで除外され、11/21～23 と12月の2回が残る
    assert len(pso2day) == 3
    assert any("レアドロップ倍率+100%" in payload["description"] for payload in pso2day)
    assert all(PSO2_DAY_URL in payload["description"] for payload in pso2day)
    assert any(kind == "new" for kind, _, _ in notifiers["111"].sent)


def test_guild_failure_does_not_block_others():
    store, apis, notifiers, run = make_run(
        [("111", "chan-A", False), ("222", "chan-B", True)]
    )

    summary = run()

    assert summary.failed == 1
    assert notifiers["111"].sent  # 失敗したサーバー以外は通知できている
    assert notifiers["222"].sent == []

    # 失敗は last_error に記録され、フラグは立っていない（次回再試行）
    guild = {g.guild_id: g for g in store.list_guilds()}["222"]
    assert "投稿が拒否されました" in guild.last_error
    state_b = store.load_state("222", NOW)
    assert state_b.snapshot()["seen"] == {}

    # 次回は 222 だけ再通知する
    notifiers["111"].sent.clear()
    notifiers["222"].fail = False
    second = run()
    assert notifiers["111"].sent == []  # 111 は再通知しない
    assert notifiers["222"].sent != []


def test_env_fallback_target_when_no_registered_guilds():
    service = FakeTableService()
    store = TableStore("unused", service=service)
    store.ensure_tables()
    config = make_config(guild_id="333", channel_id="chan-C")

    targets = resolve_targets(config, store)
    assert [t.guild_id for t in targets] == ["333"]

    # Table に登録があればそちらが優先
    store.upsert_guild(GuildConfig(guild_id="444", channel_id="chan-D"))
    targets = resolve_targets(config, store)
    assert [t.guild_id for t in targets] == ["444"]


def test_disabled_guild_is_skipped():
    service = FakeTableService()
    store = TableStore("unused", service=service)
    store.ensure_tables()
    store.upsert_guild(GuildConfig(guild_id="111", channel_id="c1", enabled=True))
    store.upsert_guild(GuildConfig(guild_id="222", channel_id="c2", enabled=False))

    targets = resolve_targets(make_config(), store)
    assert [t.guild_id for t in targets] == ["111"]


def test_missing_bot_token_does_nothing():
    _, _, _, run = make_run([("111", "chan-A", False)])

    summary = run(config=make_config(bot_token=""))

    assert summary.guilds == 0
    assert summary.notified == 0


# ------------------------------------------------------------------ Phase 3
def test_kicked_guild_is_disabled_and_skipped():
    """Bot が抜けたサーバー（404/403）は無効化して、他のサーバーは続行する。"""
    store, apis, notifiers, run = make_run(
        [("111", "chan-A", False), ("222", "chan-B", False)],
        guild_status={"222": 404},
    )

    summary = run()

    assert summary.guilds == 1
    assert notifiers["111"].sent != []
    assert notifiers["222"].sent == []
    assert apis["222"].created == []

    guilds = {g.guild_id: g for g in store.list_guilds()}
    assert guilds["222"].enabled is False
    assert "アクセスできません" in guilds["222"].last_error

    # 無効化されたサーバーは次回の対象にならない
    from cloud.runner import resolve_targets

    assert [t.guild_id for t in resolve_targets(make_config(), store)] == ["111"]


def test_guild_forbidden_is_disabled():
    store, _, notifiers, run = make_run(
        [("111", "chan-A", False)], guild_status={"111": 403}
    )
    summary = run()
    assert summary.guilds == 0
    assert {g.guild_id: g for g in store.list_guilds()}["111"].enabled is False


def test_transient_guild_error_keeps_guild_active():
    """一時的な 5xx では無効化しない（次回も対象に残る）。"""
    store, _, notifiers, run = make_run(
        [("111", "chan-A", False)], guild_status={"111": 500}
    )
    summary = run()

    assert summary.guilds == 1
    assert notifiers["111"].sent != []
    assert {g.guild_id: g for g in store.list_guilds()}["111"].enabled is True


def test_send_failure_records_http_status():
    store, _, notifiers, run = make_run([("111", "chan-A", True)])

    summary = run()

    assert summary.failed == 1
    guild = {g.guild_id: g for g in store.list_guilds()}["111"]
    assert "投稿が拒否されました (HTTP 403)" in guild.last_error
