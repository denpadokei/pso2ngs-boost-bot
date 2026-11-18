"""メンテナンス予定の取得。

告知記事の「サーバーメンテナンススケジュール」には

    12月2日（水）8:00 ～ 17:00

のように未来の予定が並ぶ。これを読み取り、期間の
「メンテナンス開始 / 終了」を実際の時刻に解決する。
取得できなかった場合は従来どおり 11:00 を仮置きする。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from .fetch import SITE, FetchError, Fetcher
from .periods import Point
from .tz import JST

log = logging.getLogger(__name__)

MAINTENANCE_LIST_URL = SITE + "/players/news/maintenance/"

_TIME_RANGE = re.compile(
    r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日\s*(?:\([月火水木金土日]\))?\s*"
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2})\s*[~〜～]\s*"
    r"(?P<eh>\d{1,2}):(?P<em>\d{2})"
)


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    @property
    def day(self) -> date:
        return self.start.date()


class MaintenanceCalendar:
    def __init__(self, windows: list[Window] | None = None) -> None:
        self.windows = sorted(windows or [], key=lambda window: window.start)
        self._by_day = {window.day: window for window in self.windows}

    def __len__(self) -> int:
        return len(self.windows)

    def resolve(self, point: Point) -> Point:
        """「メンテナンス開始 / 終了」の注記を実際の時刻に置き換える。"""
        if point.at is None or not point.note:
            return point

        window = self._by_day.get(point.at.date())
        if window is None:
            return point

        note = point.note
        if "メンテナンス開始" in note or "メンテ開始" in note:
            return replace(point, at=window.start, has_time=True)
        if "メンテナンス終了" in note or "メンテ終了" in note:
            return replace(point, at=window.end, has_time=True)
        return point

    def describe(self) -> str:
        return ", ".join(
            f"{window.start:%m/%d %H:%M}-{window.end:%H:%M}" for window in self.windows
        )


def parse_calendar_html(html: str, now: datetime) -> MaintenanceCalendar:
    soup = BeautifulSoup(html, "html.parser")
    text = unicodedata.normalize("NFKC", soup.get_text("\n"))

    windows: list[Window] = []
    seen: set[date] = set()
    for match in _TIME_RANGE.finditer(text):
        month = int(match.group("month"))
        day = int(match.group("day"))
        year = now.year
        try:
            start = datetime(
                year, month, day, int(match.group("sh")), int(match.group("sm")), tzinfo=JST
            )
            end = datetime(
                year, month, day, int(match.group("eh")), int(match.group("em")), tzinfo=JST
            )
        except ValueError:
            continue

        # 年またぎ（12月の告知に1月の予定が載るなど）
        if start < now - timedelta(days=180):
            start = start.replace(year=year + 1)
            end = end.replace(year=year + 1)
        if end < start:
            end += timedelta(days=1)
        if start.date() in seen:
            continue
        seen.add(start.date())
        windows.append(Window(start=start, end=end))

    return MaintenanceCalendar(windows)


def fetch_calendar(fetcher: Fetcher, now: datetime, limit: int = 3) -> MaintenanceCalendar:
    """メンテナンス一覧から新しい告知を拾い、予定表を読む。"""
    try:
        listing = fetcher.get(MAINTENANCE_LIST_URL)
    except FetchError as exc:
        log.warning("メンテナンス一覧を取得できませんでした: %s", exc)
        return MaintenanceCalendar()

    soup = BeautifulSoup(listing, "html.parser")
    links: list[str] = []
    for anchor in soup.select("ul.topicList li a[href]"):
        href = anchor["href"]
        if "/players/news/" in href and href not in links:
            links.append(href)

    for href in links[:limit]:
        try:
            page = fetcher.get(href)
        except FetchError:
            continue
        calendar = parse_calendar_html(page, now)
        if len(calendar):
            return calendar

    log.info("メンテナンス予定表が見つかりませんでした（11:00 仮置きで動作します）")
    return MaintenanceCalendar()
