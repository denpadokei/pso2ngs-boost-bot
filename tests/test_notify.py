from datetime import datetime

from pso2boost.models import BoostEvent, event_key
from pso2boost.notify import build_embed, render_text
from pso2boost.periods import parse_period
from pso2boost.tz import JST

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=JST)

PERIOD = parse_period("11/11 12:00～11/18 メンテナンス開始まで", 2026)


def make_event() -> BoostEvent:
    return BoostEvent(
        key=event_key("ブーストキャンペーン 第1弾", PERIOD),
        label="ブーストキャンペーン 第1弾",
        period=PERIOD,
        url="https://pso2.jp/players/news/32486/",
        description="コスチュームGETキャンペーンに関連したクエストでブースト効果が発生！",
        quests=["アークス双陣杯", "星砕の暴進"],
        buffs_all=["レアドロップ倍率+100%", "プリセット能力付与倍率+50%"],
        buffs_premium=["レアドロップ倍率+50%", "プリセット能力付与倍率+50%"],
    )


def test_discal_layout_matches_reference():
    embed = build_embed(make_event(), "new", NOW)
    assert embed["title"] == "!~Event Announcement~!"
    names = [field["name"] for field in embed["fields"]]
    assert names[:5] == ["Name", "Description", "Start (Local)", "End (Local)", "Location"]
    assert "Event ID" in names
    assert embed["footer"]["text"].startswith("Announcement ID: ")

    values = {field["name"]: field["value"] for field in embed["fields"]}
    assert values["Name"] == "ブーストキャンペーン 第1弾"
    assert values["Description"] == "・レアドロップ倍率+100%\n・プリセット能力付与倍率+50%"
    assert values["Start (Local)"] == "2026年11月11日 水曜日 12:00"
    assert values["End (Local)"] == "2026年11月18日 水曜日 メンテナンス開始"
    assert values["Location"] == "アークス双陣杯 / 星砕の暴進"


def test_inline_pairs():
    embed = build_embed(make_event(), "new", NOW)
    inline = {field["name"]: field["inline"] for field in embed["fields"]}
    assert inline["Start (Local)"] is True
    assert inline["End (Local)"] is True
    assert inline["Location"] is False


def test_location_falls_back_to_all_quests():
    event = make_event()
    event.quests = []
    embed = build_embed(event, "new", NOW)
    values = {field["name"]: field["value"] for field in embed["fields"]}
    assert values["Location"] == "すべてのクエスト"


def test_reminder_marks_today():
    start_embed = build_embed(make_event(), "start", NOW)
    values = {field["name"]: field["value"] for field in start_embed["fields"]}
    assert values["Description"].startswith("【本日から】")

    end_embed = build_embed(make_event(), "end", NOW)
    values = {field["name"]: field["value"] for field in end_embed["fields"]}
    assert values["Description"].startswith("【本日まで】")


def test_end_after_the_time_switches_to_past_tense():
    late = datetime(2026, 11, 18, 13, 0, tzinfo=JST)
    embed = build_embed(make_event(), "end", late)
    values = {field["name"]: field["value"] for field in embed["fields"]}
    assert values["Description"].startswith("【終了】")

    compact = build_embed(make_event(), "end", late, style="compact")
    assert "終了しました" in compact["description"]


def test_premium_section_only_when_present():
    embed = build_embed(make_event(), "new", NOW)
    assert any(field["name"] == "Premium" for field in embed["fields"])

    event = make_event()
    event.buffs_premium = []
    embed = build_embed(event, "new", NOW)
    assert not any(field["name"] == "Premium" for field in embed["fields"])


def test_compact_style_still_available():
    embed = build_embed(make_event(), "new", NOW, style="compact")
    assert embed["title"].startswith("🆕")
    assert embed["fields"][0]["name"] == "開催期間"


def test_render_text_is_readable():
    text = render_text(make_event(), "start")
    assert "!~Event Announcement~!" in text
    assert "Name: ブーストキャンペーン 第1弾" in text
    assert "[PSO2NGS 公式サイト](https://pso2.jp/players/news/32486/)" in text


def test_field_value_is_truncated():
    event = make_event()
    event.buffs_all = ["x" * 200] * 10
    embed = build_embed(event, "new", NOW)
    field = next(f for f in embed["fields"] if f["name"] == "Description")
    assert len(field["value"]) <= 1024


def test_event_link_field():
    embed = build_embed(
        make_event(), "new", NOW, event_url="https://discord.com/events/1/2"
    )
    field = next(f for f in embed["fields"] if f["name"] == "イベント")
    assert "https://discord.com/events/1/2" in field["value"]


class FakeResponse:
    def __init__(self, status: int = 204) -> None:
        self.status_code = status
        self.text = ""
        self.content = b""

    def json(self):
        return {}


class FakeSession:
    def __init__(self, status: int = 204) -> None:
        self.status = status
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(self.status)


def test_webhook_payload_includes_username_and_mention():
    from pso2boost.notify import DiscordNotifier

    session = FakeSession()
    notifier = DiscordNotifier(
        webhook_url="https://discord.com/api/webhooks/1/abc",
        mention_role_id="42",
        username="PSO2NGS",
        session=session,
    )
    assert notifier.enabled
    notifier.send_event(make_event(), "new")

    call = session.calls[0]
    assert call["url"].endswith("/webhooks/1/abc")
    assert call["json"]["username"] == "PSO2NGS"
    assert call["json"]["content"] == "<@&42>"
    assert call["headers"] == {}


def test_bot_token_posts_to_channel():
    from pso2boost.notify import DiscordNotifier

    session = FakeSession()
    notifier = DiscordNotifier(
        bot_token="tok", channel_id="999", session=session
    )
    assert notifier.enabled
    notifier.send_event(make_event(), "start")

    call = session.calls[0]
    assert call["url"].endswith("/channels/999/messages")
    assert call["headers"]["Authorization"] == "Bot tok"
    assert "username" not in call["json"]


def test_notifier_disabled_without_destination():
    from pso2boost.notify import DiscordNotifier

    assert not DiscordNotifier().enabled
    assert not DiscordNotifier(bot_token="tok").enabled
    assert not DiscordNotifier(webhook_url="https://example.com", dry_run=True).enabled


def test_send_event_uses_given_now():
    from pso2boost.notify import DiscordNotifier

    session = FakeSession()
    notifier = DiscordNotifier(webhook_url="https://example.com/hook", session=session)
    notifier.send_event(
        make_event(), "end", now=datetime(2026, 11, 18, 13, 0, tzinfo=JST)
    )

    embed = session.calls[0]["json"]["embeds"][0]
    description = next(f["value"] for f in embed["fields"] if f["name"] == "Description")
    assert description.startswith("【終了】")


class SequenceSession:
    """レスポンスを順に返す Fake。429 の再試行テストに使う。"""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.calls = 0

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        return FakeResponse(self.statuses.pop(0))


def test_rate_limit_is_retried_once(monkeypatch):
    from pso2boost import notify as notify_module
    from pso2boost.notify import DiscordNotifier

    slept: list[float] = []
    monkeypatch.setattr(notify_module.time, "sleep", slept.append)
    session = SequenceSession([429, 204])
    notifier = DiscordNotifier(webhook_url="https://example.com/hook", session=session)

    assert notifier.send_event(make_event(), "new")
    assert session.calls == 2
    assert slept == [1.0]  # retry_after が無いときは既定の1秒
    assert notifier.last_status == 204


def test_failed_post_records_status():
    from pso2boost.notify import DiscordNotifier

    session = SequenceSession([403])
    notifier = DiscordNotifier(webhook_url="https://example.com/hook", session=session)

    assert not notifier.send_event(make_event(), "new")
    assert notifier.last_status == 403
