"""RSS から新しい告知記事を見つけ、ブースト情報を拾う。

スケジュールページは期間つきで便利だが、掲載されないブーストもある。
RSS で新着記事を追い、記事本文にブーストの表があればイベント化する。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .article import events_from_article, parse_article_html
from .fetch import FetchError, Fetcher
from .models import BoostEvent
from .rss import RSS_URL, parse_rss
from .state import State
from .tz import to_jst

log = logging.getLogger(__name__)

DEFAULT_MAX_AGE_DAYS = 14


def discover_from_rss(
    fetcher: Fetcher,
    state: State,
    now: datetime,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    calendar=None,
) -> list[BoostEvent]:
    try:
        xml_text = fetcher.get(RSS_URL)
    except FetchError as exc:
        log.warning("RSS を取得できませんでした: %s", exc)
        return []

    events: list[BoostEvent] = []
    for item in parse_rss(xml_text):
        if not item.is_site_page:
            continue
        if item.published is not None:
            published = to_jst(item.published)
            if now - published > timedelta(days=max_age_days):
                continue
        if state.rss_seen(item.link):
            continue

        try:
            html = fetcher.get(item.link)
        except FetchError as exc:
            log.warning("記事を取得できませんでした (%s): %s", item.link, exc)
            continue

        state.mark_rss(item.link)
        article = parse_article_html(html, item.link, now, calendar)
        found = events_from_article(article)
        if found:
            log.info("RSS から %d 件のブーストを検出: %s", len(found), item.link)
        events.extend(found)

    return events
