"""イベントスケジュール（/players/schedule/）の解析。

このページはブーストキャンペーンを 1 行ずつ期間つきで載せてくれるため、
「新着検知」と「開始・終了リマインド」の主ソースとして使う。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

from .fetch import SCHEDULE_URL
from .models import BoostEvent, event_key
from .periods import normalize, parse_period

if TYPE_CHECKING:  # pragma: no cover
    from .maintenance import MaintenanceCalendar

DEFAULT_KEYWORDS = ("ブースト", "強化応援")

_YEAR_RE = re.compile(r"(\d{4})/")


def _reference_year(soup: BeautifulSoup, now: datetime) -> int:
    date_range = soup.select_one(".period-date")
    if date_range:
        match = _YEAR_RE.search(date_range.get_text(" ", strip=True))
        if match:
            return int(match.group(1))
    return now.year


def _cell_text(element) -> str:
    return normalize(element.get_text(" ", strip=True)) if element else ""


def parse_schedule_html(
    html: str,
    now: datetime,
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS,
    page_url: str = SCHEDULE_URL,
    calendar: "MaintenanceCalendar | None" = None,
) -> list[BoostEvent]:
    soup = BeautifulSoup(html, "html.parser")
    default_year = _reference_year(soup, now)
    events: list[BoostEvent] = []
    seen: set[str] = set()

    for item in soup.select("li.list"):
        title_element = item.select_one("strong")
        period_element = item.select_one(".eventPeriod")
        if title_element is None or period_element is None:
            continue

        label = _cell_text(title_element)
        if not label or not any(keyword in label for keyword in keywords):
            continue

        period = parse_period(_cell_text(period_element), default_year, now, calendar)
        if period.start.at is None and period.end.at is None:
            continue

        link = item.select_one("a[href]")
        url = link["href"] if link else None

        key = event_key(label, period)
        if key in seen:
            continue
        seen.add(key)

        events.append(
            BoostEvent(
                key=key,
                label=label,
                period=period,
                url=url,
                source="schedule",
            )
        )

    return events


def filter_relevant(events: list[BoostEvent], now: datetime) -> list[BoostEvent]:
    """終わったイベントと、明らかに古い「未定」を落とす。"""
    result: list[BoostEvent] = []
    for event in events:
        period = event.period
        if period.end.at is not None:
            if (now - period.end.at).days > 1:
                continue
        elif period.start.at is not None and (now - period.start.at).days > 365:
            # 終了未定のまま1年以上前のものはスケジュールの残骸とみなす
            continue
        result.append(event)
    return result
