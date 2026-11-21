"""公式番組『NGS ヘッドライン WAVE』の配信予定を検知する。

RSS の MEDIA 項目はリンク先が YouTube Live なので、既存の ``is_site_page``
チェックでは除外されていた。配信ページの ``scheduledStartTime`` から開始時刻を
読み取る。番組には終了時刻の明記がないため、開始から1時間を終了時刻とする。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from .models import BoostEvent, event_key
from .periods import Period, Point, normalize
from .rss import FeedItem
from .tz import JST

log = logging.getLogger(__name__)

HEADLINE_KEYWORD = "ヘッドライン"
DEFAULT_DURATION = timedelta(hours=1)
# 配信ページが読めないときの保険（番組の定型開始時刻）。
FALLBACK_START_HOUR = 21

_START_RE = re.compile(r'"scheduledStartTime"\s*:\s*"(\d{10,13})"')
_TITLE_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})")


def is_headline_item(item: FeedItem) -> bool:
    """RSS 項目が NGSヘッドラインの告知かどうか。"""
    return HEADLINE_KEYWORD in item.title


def start_from_page(html: str) -> datetime | None:
    """YouTube の配信ページから scheduledStartTime（Unix 秒）を読む。"""
    match = _START_RE.search(html)
    if match is None:
        return None
    value = int(match.group(1))
    if value > 10_000_000_000:  # ミリ秒で来る場合
        value //= 1000
    try:
        return datetime.fromtimestamp(value, JST)
    except (ValueError, OSError, OverflowError):
        return None


def start_from_title(item: FeedItem) -> datetime | None:
    """タイトルの「11/24（火）」と告知日から開始日時を推定する。"""
    match = _TITLE_DATE_RE.search(item.title)
    if match is None:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    year = item.published.year if item.published is not None else datetime.now(JST).year
    if item.published is not None and month < item.published.month - 6:
        year += 1  # 12月に翌年1月の番組を告知する場合
    try:
        return datetime(year, month, day, FALLBACK_START_HOUR, 0, tzinfo=JST)
    except ValueError:
        return None


def event_from_headline(item: FeedItem, html: str, now: datetime) -> BoostEvent | None:
    """告知項目と配信ページから1件のイベントを作る。開始時刻不明なら None。"""
    start = start_from_page(html) or start_from_title(item)
    if start is None:
        log.warning("NGSヘッドラインの開始時刻を特定できませんでした: %s", item.link)
        return None

    end = start + DEFAULT_DURATION
    label = normalize(item.title) or "NGSヘッドライン"
    period = Period(
        start=Point(at=start, has_time=True),
        end=Point(at=end, has_time=True),
        raw=f"{start:%Y/%m/%d %H:%M}～+1h",
    )
    return BoostEvent(
        key=event_key(label, period),
        label=label,
        period=period,
        url=item.link,
        description="終了時刻の明記がないため、開始から1時間で設定しています。",
        source="headline",
    )
