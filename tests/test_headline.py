from datetime import datetime
from pathlib import Path

from pso2boost.discover import discover_from_rss
from pso2boost.fetch import FetchError
from pso2boost.headline import (
    event_from_headline,
    is_headline_item,
    start_from_page,
    start_from_title,
)
from pso2boost.rss import FeedItem, parse_rss
from pso2boost.state import State
from pso2boost.tz import JST

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 21, 9, 0, tzinfo=JST)
ITEM = FeedItem(
    title="11/24（火）公式番組『NGS ヘッドライン WAVE』",
    link="https://youtube.com/live/NhXOlE19k8k",
    published=datetime(2026, 11, 17, 11, 0, tzinfo=JST),
)


class FakeFetcher:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.requested: list[str] = []

    def get(self, url: str, cache: bool = False) -> str:
        self.requested.append(url)
        if url not in self.mapping:
            raise FetchError(f"not in fixture: {url}")
        return self.mapping[url]


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_is_headline_item():
    assert is_headline_item(ITEM) is True
    assert is_headline_item(FeedItem("コスチュームGETキャンペーン", "https://pso2.jp/x/")) is False


def test_start_from_page_reads_youtube_schedule():
    start = start_from_page(load("youtube_headline.html"))

    assert start == datetime(2026, 11, 24, 21, 0, tzinfo=JST)


def test_start_from_title_falls_back_to_program_time():
    assert start_from_title(ITEM) == datetime(2026, 11, 24, 21, 0, tzinfo=JST)

    # 12月に翌年1月の番組を告知するケース
    january = FeedItem(
        title="1/27（火）公式番組『NGS ヘッドライン WAVE』",
        link="https://youtube.com/live/x",
        published=datetime(2026, 12, 22, 15, 0, tzinfo=JST),
    )
    assert start_from_title(january) == datetime(2027, 1, 27, 21, 0, tzinfo=JST)


def test_event_ends_one_hour_after_start():
    event = event_from_headline(ITEM, load("youtube_headline.html"), NOW)

    assert event is not None
    assert event.period.start.at == datetime(2026, 11, 24, 21, 0, tzinfo=JST)
    assert event.period.end.at == datetime(2026, 11, 24, 22, 0, tzinfo=JST)
    assert event.url == ITEM.link
    assert event.source == "headline"
    assert "1時間" in event.description


def test_event_falls_back_when_page_has_no_schedule():
    event = event_from_headline(ITEM, "<html></html>", NOW)

    assert event is not None
    assert event.period.start.at == datetime(2026, 11, 24, 21, 0, tzinfo=JST)
    assert event.period.end.at == datetime(2026, 11, 24, 22, 0, tzinfo=JST)


def test_discover_from_rss_includes_headline_item(tmp_path: Path):
    fetcher = FakeFetcher(
        {
            "https://pso2.jp/players/pso2news.xml": load("pso2news.xml"),
            ITEM.link: load("youtube_headline.html"),
        }
    )
    state = State(tmp_path / "state.json", NOW)

    events = discover_from_rss(fetcher, state, NOW)

    headlines = [event for event in events if event.source == "headline"]
    assert len(headlines) == 1
    assert headlines[0].period.start.at == datetime(2026, 11, 24, 21, 0, tzinfo=JST)
    assert state.rss_seen(ITEM.link)
    assert ITEM.link in fetcher.requested

    # 2回目は既読としてスキップ
    fetcher.requested.clear()
    assert discover_from_rss(fetcher, state, NOW) == []
    assert ITEM.link not in fetcher.requested


def test_rss_fixture_headline_item_is_detected():
    items = parse_rss(load("pso2news.xml"))
    headlines = [item for item in items if is_headline_item(item)]

    assert len(headlines) == 1
    assert headlines[0].link == ITEM.link
