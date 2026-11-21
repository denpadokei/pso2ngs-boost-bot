"""レビュー指摘（CRITICAL/HIGH/MEDIUM）の修正を検証する回帰テスト。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import requests

from cloud import runner
from cloud.config import CloudConfig
from cloud.discord import DiscordRestError
from cloud.storage import (
    GUILD_PARTITION,
    LOCK_PARTITION,
    LOCK_ROW,
    SESSION_PARTITION,
    GuildConfig,
    TableStore,
)
from fakes import FakeApi, FakeDiscordRest, FakeFetcher, FakeNotifier, FakeTableService
from pso2boost.discord_events import DiscordApiError, ScheduledEventsApi
from pso2boost.fetch import Fetcher, SCHEDULE_URL
from pso2boost.notify import DiscordNotifier
from pso2boost.models import BoostEvent
from pso2boost.periods import parse_period
from pso2boost.tz import now_jst
from pso2boost.rss import RSS_URL

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=None)


def make_store() -> TableStore:
    store = TableStore("unused", service=FakeTableService())
    store.ensure_tables()
    return store


def make_config(**overrides) -> CloudConfig:
    values = dict(
        bot_token="test-token",
        storage_connection="unused",
        client_id="123",
        client_secret="secret",
        host_name="example.azurewebsites.net",
    )
    values.update(overrides)
    return CloudConfig(**values)


def make_runner_context(store: TableStore, *, api_factory=None):
    for gid in ("111", "222"):
        store.upsert_guild(GuildConfig(guild_id=gid, channel_id=f"ch-{gid}"))
    fetcher = FakeFetcher({SCHEDULE_URL: "<html></html>", RSS_URL: "<rss></rss>"})
    notifiers: dict[str, FakeNotifier] = {}
    api_factory = api_factory or (lambda target: FakeApi(target.guild_id))

    def run():
        return runner.run_weekly(
            make_config(),
            store=store,
            fetcher=fetcher,
            guild_client=FakeDiscordRest(),
            api_factory=api_factory,
            notifier_factory=lambda target: notifiers.setdefault(
                target.guild_id, FakeNotifier(target.channel_id)
            ),
        )

    return run, notifiers


# ---------------------------------------------------------- CRITICAL: 例外隔離
class BoomSession:
    headers: dict = {}

    def request(self, *args, **kwargs):
        raise requests.ConnectionError("network down")


def test_request_exception_is_wrapped_as_discord_api_error():
    api = ScheduledEventsApi("token", "123", session=BoomSession())
    with pytest.raises(DiscordApiError, match="接続に失敗"):
        api.create(name="x", description="", start=NOW, end=NOW, location="l")


def test_one_guild_exception_does_not_stop_others():
    store = make_store()

    def api_factory(target):
        if target.guild_id == "111":
            raise RuntimeError("boom in api factory")
        return FakeApi(target.guild_id)

    run, notifiers = make_runner_context(store, api_factory=api_factory)
    summary = run()

    assert summary.failed == 1
    assert any("boom in api factory" in (d.last_error or "") for d in summary.details)
    guilds = {g.guild_id: g for g in store.list_guilds()}
    assert "boom in api factory" in guilds["111"].last_error
    assert guilds["222"].last_error == ""
    assert notifiers["222"].sent is not None  # 他サーバーも処理された


# -------------------------------------------------------- MEDIUM: 排他ロック
def test_lock_prevents_overlapping_runs():
    store = make_store()
    lock_now = now_jst()  # 実行時計と同じ基準を使う
    assert store.try_acquire_lock(lock_now, "owner-A") is True
    assert store.try_acquire_lock(lock_now, "owner-B") is False

    run, _notifiers = make_runner_context(store)
    summary = run()
    assert summary.status == "skipped"
    assert any("実行中" in action for action in summary.actions)

    store.release_lock("owner-B")  # 他人の所有権では解放されない
    assert run().status == "skipped"
    store.release_lock("owner-A")
    assert run().status == "ok"


def test_expired_lock_can_be_taken_over():
    store = make_store()
    assert store.try_acquire_lock(now_jst() - timedelta(minutes=20), "old") is True
    assert store.try_acquire_lock(now_jst(), "new") is True  # 失効分を奪う
    store.release_lock("old")  # 古い所有者は新しいロックを消せない
    rows = store._service.list_entities(
        store.state_table, query="PartitionKey eq '_lock'"
    )
    assert any(str(row.get("owner")) == "new" for row in rows)


# ---------------------------------------------------------- MEDIUM: 権限更新
def test_set_permissions_ok_updates_existing_row():
    store = make_store()
    store.upsert_guild(GuildConfig(guild_id="111", channel_id="ch", permissions_ok=True))

    store.set_permissions_ok("111", False)

    guild = {g.guild_id: g for g in store.list_guilds()}["111"]
    assert guild.permissions_ok is False
    assert guild.channel_id == "ch"  # MERGE で他列は保持される


# ---------------------------------------------------- MEDIUM: 壊れたセッション
def test_broken_session_is_deleted_safely():
    store = make_store()
    store._service.upsert_entity(
        store.state_table,
        {
            "PartitionKey": SESSION_PARTITION,
            "RowKey": "broken",
            "exp": "not-a-number",
            "guilds": "{broken json",
        },
    )
    assert store.get_session("broken", NOW) is None
    rows = store._service.list_entities(store.state_table)
    assert not any(row["RowKey"] == "broken" for row in rows)


def test_prune_sessions_survives_broken_exp():
    store = make_store()
    store._service.upsert_entity(
        store.state_table,
        {"PartitionKey": SESSION_PARTITION, "RowKey": "weird", "exp": "bad"},
    )
    assert store.prune_sessions(NOW) == 1


# -------------------------------------------------------------- HIGH: 再試行
def test_fetcher_retries_server_errors(monkeypatch):
    import pso2boost.fetch as fetch_module

    monkeypatch.setattr(fetch_module.time, "sleep", lambda _seconds: None)

    class Response:
        def __init__(self, status: int, text: str = "ok") -> None:
            self.status_code = status
            self.text = text
            self.encoding = "utf-8"

    class Session:
        headers: dict = {}

        def __init__(self) -> None:
            self.calls = 0

        def get(self, url, timeout=None):
            self.calls += 1
            return Response(503) if self.calls < 3 else Response(200, "recovered")

    fetcher = Fetcher(retries=3, delay=0.0)
    session = Session()
    fetcher.session = session

    assert fetcher.get(SCHEDULE_URL) == "recovered"
    assert session.calls == 3


def test_notifier_retries_server_errors(monkeypatch):
    import pso2boost.notify as notify_module

    monkeypatch.setattr(notify_module.time, "sleep", lambda _seconds: None)

    class Response:
        def __init__(self, status: int) -> None:
            self.status_code = status
            self.text = ""

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, url, json=None, headers=None, timeout=None):
            self.calls += 1
            return Response(500) if self.calls < 3 else Response(204)

    notifier = DiscordNotifier(webhook_url="https://example.com/hook")
    session = Session()
    notifier.session = session
    event = BoostEvent(
        key="k", label="l", period=parse_period("11/25 12:00～12/2 メンテナンス開始まで", 2026)
    )

    assert notifier.send_event(event, "new", now=NOW) is True
    assert session.calls == 3


# -------------------------------------- 再レビュー R1: エラーページのHTTPステータス
def test_error_page_returns_http_500_but_user_cancel_stays_200():
    import function_app as entry
    from cloud.setup_pages import Page, _error_page, render_page

    assert entry._response(Page(200, _error_page("boom"))).status_code == 500
    assert entry._response(Page(200, render_page("ok", "<h1>ok</h1>"))).status_code == 200
    assert entry._response(Page(403, _error_page("denied"))).status_code == 403


# --------------------------------------- 再レビュー N-1: Timer の失敗シグナル
def test_weekly_timer_raises_on_failed_or_partial(monkeypatch):
    import function_app as entry

    class S:
        status = "failed"
        summary = "失敗サマリ"
        actions = ["データ取得失敗"]

    class FakeConfig:
        @staticmethod
        def from_env():
            return object()

    monkeypatch.setattr(entry.cloud_config, "CloudConfig", FakeConfig)
    monkeypatch.setattr(entry.runner, "run_weekly", lambda *a, **k: S())
    with pytest.raises(RuntimeError, match="週次同期に失敗"):
        entry.weekly_sync(None)

    S.status = "ok"
    entry.weekly_sync(None)  # 成功時は例外なし

    S.status = "skipped"
    S.actions = ["別の同期が実行中です"]
    entry.weekly_sync(None)  # ロック競合は正当なスキップ


def test_health_returns_503_when_degraded(monkeypatch):
    import function_app as entry

    class C:
        bot_token = ""
        storage_connection = "cs"
        client_secret = "secret"

    class FakeConfig:
        @staticmethod
        def from_env():
            return C()

    monkeypatch.setattr(entry.cloud_config, "CloudConfig", FakeConfig)
    response = entry.health(None)
    assert response.status_code == 503
    assert "degraded" in response.get_body().decode("utf-8")


# --------------------------------------------- 再レビュー R-4: Discord REST
def test_discord_rest_normalizes_connection_error():
    from cloud.discord import DiscordRest

    class Session:
        headers: dict = {}

        def get(self, *args, **kwargs):
            raise requests.ConnectionError("offline")

    rest = DiscordRest("token", session=Session())
    with pytest.raises(DiscordRestError, match="接続に失敗"):
        rest.get_channel("123")


def test_discord_rest_retries_server_errors(monkeypatch):
    import cloud.discord as discord_module
    from cloud.discord import DiscordRest

    monkeypatch.setattr(discord_module.time, "sleep", lambda _seconds: None)

    class Response:
        def __init__(self, status: int) -> None:
            self.status_code = status
            self.text = ""

        def json(self):
            return {"id": "1"}

    class Session:
        headers: dict = {}

        def __init__(self) -> None:
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return Response(503) if self.calls < 3 else Response(200)

    session = Session()
    rest = DiscordRest("token", session=session)
    assert rest.get_channel("123") == {"id": "1"}
    assert session.calls == 3


# ------------------------------- 再レビュー R-6: Discord追跡の終了後保持
def test_discord_tracking_survives_until_end_plus_retention(tmp_path):
    from pso2boost.state import State

    now = datetime(2026, 11, 18, 12, 0, tzinfo=None).replace(
        tzinfo=__import__("pso2boost.tz", fromlist=["JST"]).JST
    )
    state = State(tmp_path / "state.json", now)
    # updated は 40 日前でも、end が 5 日前なら追跡を残す
    state.set_discord_event(
        "k",
        {"id": "ev1", "end": (now - __import__("datetime").timedelta(days=5)).isoformat()},
    )
    state._data["discord"]["k"]["updated"] = (
        now - __import__("datetime").timedelta(days=40)
    ).isoformat()

    state.prune(days=30)
    assert state.discord_event("k") is not None
