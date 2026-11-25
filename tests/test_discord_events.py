import json

import pytest

from pso2boost import discord_events
from pso2boost.discord_events import DiscordApiError, ScheduledEventsApi


class FakeResponse:
    def __init__(self, status: int = 200, payload=None) -> None:
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
        self.content = self.text.encode("utf-8") if self.text else b""

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict | None]] = []
        self.headers: dict[str, str] = {}

    def request(self, method, url, json=None, timeout=None):
        self.calls.append((method, url, json))
        return self.responses.pop(0)


def api_with(responses) -> tuple[ScheduledEventsApi, FakeSession]:
    session = FakeSession(responses)
    api = ScheduledEventsApi("token", "123", session=session, timeout=1)
    return api, session


def test_create_sends_external_event_payload():
    api, session = api_with([FakeResponse(200, {"id": "999"})])
    from datetime import datetime

    from pso2boost.tz import JST

    event_id = api.create(
        name="テスト",
        description="説明",
        start=datetime(2026, 11, 25, 12, 0, tzinfo=JST),
        end=datetime(2026, 12, 2, 11, 0, tzinfo=JST),
        location="すべてのクエスト",
    )

    assert event_id == "999"
    method, url, payload = session.calls[0]
    assert method == "POST"
    assert url.endswith("/guilds/123/scheduled-events")
    assert payload["entity_type"] == 3
    assert payload["privacy_level"] == 2
    assert payload["entity_metadata"] == {"location": "すべてのクエスト"}
    assert payload["scheduled_start_time"].endswith("+09:00")
    assert payload["scheduled_end_time"]


def test_update_details_sends_no_scheduled_times():
    """開始済みイベントには時刻を送らず、場所だけを部分更新する。"""
    api, session = api_with([FakeResponse(204)])
    api.update_details(
        "999",
        name="x" * 120,
        description="説明",
        location="場所",
    )

    method, url, payload = session.calls[0]
    assert method == "PATCH"
    assert url.endswith("/guilds/123/scheduled-events/999")
    assert payload["entity_metadata"] == {"location": "場所"}
    assert payload["description"] == "説明"
    assert len(payload["name"]) == 100
    assert "scheduled_start_time" not in payload
    assert "scheduled_end_time" not in payload
    assert "entity_type" not in payload


def test_delete_accepts_204():
    api, session = api_with([FakeResponse(204)])
    api.delete("999")
    assert session.calls[0][0] == "DELETE"


def test_rate_limit_is_retried(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(discord_events.time, "sleep", slept.append)
    api, session = api_with(
        [
            FakeResponse(429, {"retry_after": 0.5}),
            FakeResponse(200, {"id": "1"}),
        ]
    )
    from datetime import datetime

    from pso2boost.tz import JST

    event_id = api.create(
        name="n",
        description="",
        start=datetime(2026, 11, 25, 12, 0, tzinfo=JST),
        end=datetime(2026, 12, 2, 11, 0, tzinfo=JST),
        location="l",
    )
    assert event_id == "1"
    assert slept == [0.5]


def test_server_error_is_retried_then_succeeds(monkeypatch):
    """5xx は指数バックオフで再試行する（Phase 3: 429/5xx 再試行）。"""
    slept: list[float] = []
    monkeypatch.setattr(discord_events.time, "sleep", slept.append)
    api, session = api_with(
        [
            FakeResponse(503),
            FakeResponse(502),
            FakeResponse(200, {"id": "7"}),
        ]
    )
    from datetime import datetime

    from pso2boost.tz import JST

    event_id = api.create(
        name="n",
        description="",
        start=datetime(2026, 11, 25, 12, 0, tzinfo=JST),
        end=datetime(2026, 12, 2, 11, 0, tzinfo=JST),
        location="l",
    )
    assert event_id == "7"
    assert len(session.calls) == 3
    assert slept == [2, 4]


def test_error_raises_with_body():
    api, _ = api_with([FakeResponse(403, {"message": "Missing Permissions"})])
    with pytest.raises(DiscordApiError) as excinfo:
        api.delete("1")
    assert "403" in str(excinfo.value)
    assert "Missing Permissions" in str(excinfo.value)
    assert excinfo.value.status_code == 403


def test_invite_url_contains_recommended_permissions():
    url = discord_events.invite_url("42")
    assert "client_id=42" in url
    assert f"permissions={discord_events.RECOMMENDED_PERMISSIONS}" in url
