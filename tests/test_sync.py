from datetime import datetime
from pathlib import Path

from pso2boost.models import BoostEvent, event_key
from pso2boost.periods import parse_period
from pso2boost.state import State
from pso2boost.sync import build_payload, sync_events
from pso2boost.tz import JST

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=JST)


class FakeApi:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self._counter = 0

    def event_url(self, event_id: str) -> str:
        return f"https://discord.com/events/123/{event_id}"

    def create(self, **kwargs) -> str:
        self._counter += 1
        self.created.append(kwargs)
        return f"ev{self._counter}"

    def update(self, event_id: str, **kwargs) -> None:
        self.updated.append((event_id, kwargs))

    def delete(self, event_id: str) -> None:
        self.deleted.append(event_id)


def make_event(label: str, period_text: str) -> BoostEvent:
    period = parse_period(period_text, 2026)
    return BoostEvent(
        key=event_key(label, period),
        label=label,
        period=period,
        url="https://pso2.jp/players/news/32486/",
        quests=["アークス双陣杯"],
        buffs_all=["レアドロップ倍率+100%"],
    )


def test_creates_event_for_future_period(tmp_path: Path):
    event = make_event("ブーストキャンペーン 第2弾", "11/25 12:00～12/2 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)
    api = FakeApi()

    result = sync_events([event], state, api, NOW)

    assert result.created == 1
    assert len(api.created) == 1
    payload = api.created[0]
    assert payload["name"] == "ブーストキャンペーン 第2弾"
    assert payload["location"] == "アークス双陣杯"
    assert "レアドロップ倍率+100%" in payload["description"]
    assert result.links[event.key] == "https://discord.com/events/123/ev1"
    assert state.discord_event(event.key)["id"] == "ev1"


def test_second_run_does_not_update_unchanged(tmp_path: Path):
    event = make_event("ブーストキャンペーン 第2弾", "11/25 12:00～12/2 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)
    api = FakeApi()

    sync_events([event], state, api, NOW)
    result = sync_events([event], state, api, NOW)

    assert result.created == 0
    assert result.updated == 0
    assert api.updated == []


def test_updates_when_content_changes(tmp_path: Path):
    event = make_event("ブーストキャンペーン 第2弾", "11/25 12:00～12/2 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)
    api = FakeApi()
    sync_events([event], state, api, NOW)

    event.buffs_all.append("メセタブースト+50%")
    result = sync_events([event], state, api, NOW)

    assert result.updated == 1
    assert api.updated[0][0] == "ev1"


def test_skips_already_started_event(tmp_path: Path):
    event = make_event("ブーストキャンペーン 第1弾", "11/11 12:00～11/18 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)
    api = FakeApi()

    result = sync_events([event], state, api, NOW)

    assert result.skipped == 1
    assert api.created == []


def test_deletes_after_retention(tmp_path: Path):
    event = make_event("ブーストキャンペーン 第1弾", "11/11 12:00～11/18 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)
    state.set_discord_event(event.key, {"id": "ev1", "signature": "x"})
    api = FakeApi()

    result = sync_events([event], state, api, NOW, retention_days=0)

    assert result.deleted == 1
    assert api.deleted == ["ev1"]
    assert state.discord_event(event.key) is None


def test_build_payload_includes_premium_and_link():
    event = make_event("ブーストキャンペーン 第2弾", "11/25 12:00～12/2 メンテナンス開始まで")
    event.buffs_premium = ["レアドロップ倍率+50%"]
    payload, signature = build_payload(event, NOW)
    assert "プレミアムセット有効期間中" in payload["description"]
    assert "https://pso2.jp/players/news/32486/" in payload["description"]
    assert len(signature) == 16
    # Discord クライアントは datetime を受け取り、内部で ISO8601 に変換する
    assert isinstance(payload["start"], datetime)
    assert isinstance(payload["end"], datetime)


def test_dry_run_does_not_touch_state_or_api(tmp_path: Path):
    event = make_event("ブーストキャンペーン 第2弾", "11/25 12:00～12/2 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)
    api = FakeApi()

    result = sync_events([event], state, api, NOW, dry_run=True)

    assert result.created == 1  # 「作成する予定」として数える
    assert api.created == []
    assert state.discord_event(event.key) is None


def test_deletes_event_that_left_the_schedule(tmp_path: Path):
    state = State(tmp_path / "state.json", NOW)
    state.set_discord_event(
        "old",
        {"id": "ev9", "name": "古いイベント", "end": "2026-11-01T11:00:00+09:00"},
    )
    api = FakeApi()

    result = sync_events([], state, api, NOW)

    assert result.deleted == 1
    assert api.deleted == ["ev9"]
    assert state.discord_event("old") is None


class NotFoundApi(FakeApi):
    def update(self, event_id, **kwargs) -> None:
        from pso2boost.discord_events import DiscordApiError

        raise DiscordApiError("HTTP 404: Unknown Guild Scheduled Event")

    def delete(self, event_id: str) -> None:
        from pso2boost.discord_events import DiscordApiError

        raise DiscordApiError("HTTP 404: Unknown Guild Scheduled Event")


def test_recreates_event_when_discord_returns_404(tmp_path: Path):
    event = make_event("ブーストキャンペーン 第2弾", "11/25 12:00～12/2 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)
    state.set_discord_event(event.key, {"id": "stale", "signature": "old"})
    api = NotFoundApi()

    result = sync_events([event], state, api, NOW)

    assert result.created == 1
    assert api.created
    assert state.discord_event(event.key)["id"] == "ev1"
