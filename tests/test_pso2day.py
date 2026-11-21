from datetime import datetime
from pathlib import Path

from pso2boost.fetch import FetchError
from pso2boost.pso2day import (
    PSO2_DAY_URL,
    Template,
    Window,
    discover_pso2_day,
    events_from_template,
    parse_page,
)
from pso2boost.sync import build_payload
from pso2boost.tz import JST

FIXTURE = Path(__file__).parent / "fixtures" / "article_31542.html"


class FakeFetcher:
    def __init__(self, html: str | None = None, error: Exception | None = None) -> None:
        self.html = html
        self.error = error
        self.urls: list[str] = []

    def get(self, url: str, cache: bool = False) -> str:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        assert self.html is not None
        return self.html


def load() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_parse_page_reads_windows_and_buffs():
    template = parse_page(load())

    assert len(template.windows) == 2
    assert (template.windows[0].start_day, template.windows[0].end_day) == (1, 3)
    assert (template.windows[1].start_day, template.windows[1].end_day) == (21, 23)
    assert template.windows[0].start_time == (0, 0)
    assert template.windows[0].end_time == (23, 59)
    assert "レアドロップ倍率+100%" in template.buffs_all
    assert "レアドロップ倍率+50%" in template.buffs_premium
    assert "プリセット能力付与倍率+50%" in template.buffs_premium


def test_parse_page_falls_back_when_block_missing():
    template = parse_page("<html><body>no campaign block</body></html>")

    assert template.windows == Template.defaults().windows
    assert template.buffs_all == Template.defaults().buffs_all


def test_events_cover_current_and_next_month():
    now = datetime(2026, 11, 18, 12, 0, tzinfo=JST)
    events = events_from_template(parse_page(load()), now)

    assert len(events) == 4
    assert [event.label for event in events] == [
        "PSO2の日（1～3日）",
        "PSO2の日（21～23日）",
        "PSO2の日（1～3日）",
        "PSO2の日（21～23日）",
    ]
    first = events[0]
    assert first.period.start.at == datetime(2026, 11, 1, 0, 0, tzinfo=JST)
    assert first.period.end.at == datetime(2026, 11, 3, 23, 59, tzinfo=JST)
    assert first.url == PSO2_DAY_URL
    assert first.source == "pso2day"
    assert "レアドロップ倍率+100%" in first.buffs_all
    assert "レアドロップ倍率+50%" in first.buffs_premium
    assert len({event.key for event in events}) == 4


def test_year_rollover_and_short_month():
    now = datetime(2026, 12, 25, tzinfo=JST)
    events = events_from_template(Template.defaults(), now)

    starts = [event.period.start.at for event in events]
    assert datetime(2027, 1, 1, 0, 0, tzinfo=JST) in starts
    assert datetime(2027, 1, 21, 0, 0, tzinfo=JST) in starts


def test_fetch_failure_uses_defaults():
    fetcher = FakeFetcher(error=FetchError("boom"))
    now = datetime(2026, 11, 18, tzinfo=JST)

    result = discover_pso2_day(fetcher, now)
    events = result.events

    assert fetcher.urls == [PSO2_DAY_URL]
    assert result.fetched is False
    assert result.parsed is False
    assert result.ok is False
    assert len(events) == 4
    assert events[0].period.start.at == datetime(2026, 11, 1, 0, 0, tzinfo=JST)
    assert "レアドロップ倍率+100%" in events[0].buffs_all


def test_discover_fetches_announcement_page():
    fetcher = FakeFetcher(html=load())
    now = datetime(2026, 11, 18, tzinfo=JST)

    result = discover_pso2_day(fetcher, now)
    events = result.events

    assert fetcher.urls == [PSO2_DAY_URL]
    assert result.fetched is True
    assert result.parsed is True
    assert result.ok is True
    assert len(events) == 4
    assert (events[1].period.start.at, events[1].period.end.at) == (
        datetime(2026, 11, 21, 0, 0, tzinfo=JST),
        datetime(2026, 11, 23, 23, 59, tzinfo=JST),
    )


def test_future_occurrence_is_syncable_to_discord():
    now = datetime(2026, 10, 25, tzinfo=JST)
    events = events_from_template(Template.defaults(), now, months=2)

    built = build_payload(events[3], now)  # 11/21～11/23

    assert built is not None
    payload, _ = built
    assert payload["name"] == "PSO2の日（21～23日）"
    assert "レアドロップ倍率+100%" in payload["description"]


def test_structure_change_is_reported_as_unparsed():
    html = '<div class="template01Block" id="cp01"><h2>PSO2の日</h2></div>'

    template = parse_page(html)

    assert template.parsed is False
    assert template.windows == Template.defaults().windows


def test_short_month_is_clamped():
    now = datetime(2026, 2, 1, tzinfo=JST)
    template = Template(windows=(Window(29, 31),))

    events = events_from_template(template, now, months=1)

    assert len(events) == 1
    assert events[0].period.start.at == datetime(2026, 2, 28, 0, 0, tzinfo=JST)
    assert events[0].period.end.at == datetime(2026, 2, 28, 23, 59, tzinfo=JST)


def test_reversed_window_is_skipped():
    template = Template(windows=(Window(23, 3),))  # 反転した期間

    assert events_from_template(template, datetime(2026, 11, 1, tzinfo=JST), months=1) == []
