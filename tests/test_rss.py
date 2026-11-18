from datetime import datetime
from pathlib import Path

from pso2boost.article import events_from_article, parse_article_html
from pso2boost.discover import discover_from_rss
from pso2boost.fetch import FetchError
from pso2boost.models import merge_events
from pso2boost.rss import RSS_URL, parse_rss
from pso2boost.schedule import parse_schedule_html
from pso2boost.state import State
from pso2boost.tz import JST

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=JST)
ARTICLE_URL = "https://pso2.jp/players/news/cgcNov26/"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_rss_items():
    items = parse_rss(load("pso2news.xml"))
    assert len(items) >= 3
    assert any(item.link == ARTICLE_URL for item in items)

    target = next(item for item in items if item.link == ARTICLE_URL)
    assert target.title
    assert target.published is not None
    assert target.published.utcoffset() is not None
    assert target.is_site_page is True


def test_external_links_are_not_site_pages():
    items = parse_rss(load("pso2news.xml"))
    external = [item for item in items if "youtube" in item.link or "bit.ly" in item.link]
    assert external
    assert all(not item.is_site_page for item in external)


def test_events_from_article_match_schedule_keys():
    article = parse_article_html(load("article_32486.html"), ARTICLE_URL, NOW)
    article_events = events_from_article(article)
    schedule_events = parse_schedule_html(load("schedule.html"), NOW)

    article_keys = {event.key for event in article_events}
    schedule_keys = {event.key for event in schedule_events}
    assert article_keys and article_keys <= schedule_keys

    merged = merge_events(schedule_events, article_events)
    assert len({event.key for event in merged}) == len(merged)


def test_merge_fills_details_from_secondary():
    article = parse_article_html(load("article_32486.html"), ARTICLE_URL, NOW)
    article_events = events_from_article(article)
    schedule_events = parse_schedule_html(load("schedule.html"), NOW)

    merged = merge_events(schedule_events, article_events)
    first = next(event for event in merged if event.label == "ブーストキャンペーン 第1弾")
    assert first.quests == ["アークス双陣杯", "星砕の暴進"]
    assert first.buffs_all


class FakeFetcher:
    """テスト用: 指定 URL だけ返し、それ以外は取得失敗にする。"""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.requested: list[str] = []

    def get(self, url: str, cache: bool = False) -> str:
        self.requested.append(url)
        if url not in self.mapping:
            raise FetchError(f"not in fixture: {url}")
        return self.mapping[url]


def test_discover_reads_new_article_once(tmp_path: Path):
    fetcher = FakeFetcher(
        {
            RSS_URL: load("pso2news.xml"),
            ARTICLE_URL: load("article_32486.html"),
        }
    )
    state = State(tmp_path / "state.json", NOW)

    events = discover_from_rss(fetcher, state, NOW)
    assert len(events) == 2
    assert {event.label for event in events} == {"ブーストキャンペーン 第1弾", "ブーストキャンペーン 第2弾"}
    assert state.rss_seen(ARTICLE_URL)

    # 2回目は同じ記事を取りに行かない
    fetcher.requested.clear()
    again = discover_from_rss(fetcher, state, NOW)
    assert again == []
    assert ARTICLE_URL not in fetcher.requested
