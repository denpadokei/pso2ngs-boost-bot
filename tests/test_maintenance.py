from datetime import datetime
from pathlib import Path

from pso2boost.fetch import FetchError
from pso2boost.maintenance import MAINTENANCE_LIST_URL, fetch_calendar, parse_calendar_html
from pso2boost.periods import parse_period
from pso2boost.tz import JST

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=JST)


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def calendar():
    return parse_calendar_html(load("maintenance_32532.html"), NOW)


def test_parses_future_schedule():
    parsed = calendar()
    assert len(parsed) == 3
    assert [window.start.day for window in parsed.windows] == [18, 2, 16]
    assert parsed.windows[0].start == datetime(2026, 11, 18, 11, 0, tzinfo=JST)
    assert parsed.windows[0].end == datetime(2026, 11, 18, 17, 0, tzinfo=JST)
    assert parsed.windows[1].start == datetime(2026, 12, 2, 8, 0, tzinfo=JST)


def test_resolves_maintenance_start_to_real_time():
    period = parse_period("11/25 12:00～12/2 メンテナンス開始まで", 2026, NOW, calendar())
    assert period.end.at == datetime(2026, 12, 2, 8, 0, tzinfo=JST)
    assert period.end.has_time is True
    assert period.end.display() == "2026/12/02 08:00 メンテナンス開始"


def test_resolves_maintenance_end_to_real_time():
    period = parse_period("12/2 メンテナンス終了～12/16 メンテナンス開始まで", 2026, NOW, calendar())
    assert period.start.at == datetime(2026, 12, 2, 17, 0, tzinfo=JST)
    assert period.end.at == datetime(2026, 12, 16, 11, 0, tzinfo=JST)


def test_falls_back_to_placeholder_without_calendar():
    period = parse_period("11/25 12:00～12/2 メンテナンス開始まで", 2026, NOW)
    assert period.end.at == datetime(2026, 12, 2, 11, 0, tzinfo=JST)
    assert period.end.has_time is False


def test_unknown_date_keeps_placeholder():
    period = parse_period("12/31 12:00～1/7 メンテナンス開始まで", 2026, NOW, calendar())
    assert period.end.at == datetime(2027, 1, 7, 11, 0, tzinfo=JST)
    assert period.end.has_time is False


class FakeFetcher:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.requested: list[str] = []

    def get(self, url: str, cache: bool = False) -> str:
        self.requested.append(url)
        if url not in self.mapping:
            raise FetchError(url)
        return self.mapping[url]


def test_fetch_calendar_uses_listing_then_article():
    listing = """
    <html><body><ul class="topicList">
      <li><a href="https://pso2.jp/players/news/site_maint/">プレイヤーズサイトのメンテナンス</a></li>
      <li><a href="https://pso2.jp/players/news/32532/">11/18 サーバーメンテナンスのお知らせ</a></li>
    </ul></body></html>
    """
    fetcher = FakeFetcher(
        {
            MAINTENANCE_LIST_URL: listing,
            "https://pso2.jp/players/news/site_maint/": "<html><body>予定なし</body></html>",
            "https://pso2.jp/players/news/32532/": load("maintenance_32532.html"),
        }
    )

    parsed = fetch_calendar(fetcher, NOW)
    assert len(parsed) == 3


def test_fetch_calendar_degrades_gracefully():
    fetcher = FakeFetcher({"https://pso2.jp/players/news/32532/": load("maintenance_32532.html")})
    # 一覧が取得できなければ空のカレンダー（11:00 仮置きで動く）
    assert len(fetch_calendar(fetcher, NOW)) == 0
