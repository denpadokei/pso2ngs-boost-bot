from datetime import datetime
from pathlib import Path

from pso2boost.schedule import filter_relevant, parse_schedule_html
from pso2boost.tz import JST

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parses_boost_rows_from_schedule():
    now = datetime(2026, 11, 18, 12, 0, tzinfo=JST)
    events = parse_schedule_html(load("schedule.html"), now)

    labels = [event.label for event in events]
    assert "ブーストキャンペーン 第1弾" in labels
    assert "ブーストキャンペーン 第2弾" in labels

    first = next(event for event in events if event.label == "ブーストキャンペーン 第1弾")
    assert first.period.start.at == datetime(2026, 11, 11, 12, 0, tzinfo=JST)
    assert first.period.end.at == datetime(2026, 11, 18, 11, 0, tzinfo=JST)
    assert first.url and first.url.endswith("/news/cgcNov26/#cp3")


def test_filter_drops_stale_undecided_entry():
    now = datetime(2026, 11, 18, 12, 0, tzinfo=JST)
    events = filter_relevant(parse_schedule_html(load("schedule.html"), now), now)
    assert all("育成支援" not in event.label for event in events)


def test_keys_are_stable_and_unique():
    now = datetime(2026, 11, 18, 12, 0, tzinfo=JST)
    first = parse_schedule_html(load("schedule.html"), now)
    second = parse_schedule_html(load("schedule.html"), now)
    assert [event.key for event in first] == [event.key for event in second]
    assert len({event.key for event in first}) == len(first)


def test_keywords_narrow_the_result():
    now = datetime(2026, 11, 18, 12, 0, tzinfo=JST)
    events = parse_schedule_html(load("schedule.html"), now, keywords=("存在しない語",))
    assert events == []


def test_enhancement_campaign_is_detected():
    now = datetime(2026, 11, 18, 12, 0, tzinfo=JST)
    events = parse_schedule_html(load("schedule.html"), now)
    assert any(event.label == "装備強化応援キャンペーン" for event in events)

    only_enhance = parse_schedule_html(load("schedule.html"), now, keywords=("強化応援",))
    assert [event.label for event in only_enhance] == ["装備強化応援キャンペーン"]
