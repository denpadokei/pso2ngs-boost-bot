"""RSS（https://pso2.jp/players/pso2news.xml）の解析。

RSS 1.0 (RDF) 形式。記事の新着を早く拾うために使う。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse
from xml.etree import ElementTree

from .fetch import SITE

log = logging.getLogger(__name__)

RSS_URL = SITE + "/players/pso2news.xml"

_RSS_NS = "http://purl.org/rss/1.0/"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_TAG_ITEM = f"{{{_RSS_NS}}}item"
_TAG_TITLE = f"{{{_RSS_NS}}}title"
_TAG_LINK = f"{{{_RSS_NS}}}link"
_TAG_DATE = f"{{{_DC_NS}}}date"


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    published: datetime | None = None

    @property
    def is_site_page(self) -> bool:
        parsed = urlparse(self.link)
        return parsed.netloc == urlparse(SITE).netloc and parsed.path.startswith("/players/")


def parse_rss(xml_text: str) -> list[FeedItem]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        log.warning("RSS のパースに失敗しました（先頭: %s）: %s", xml_text[:120], exc)
        return []

    items: list[FeedItem] = []
    for element in root.iter(_TAG_ITEM):
        link = (element.findtext(_TAG_LINK) or "").strip()
        if not link:
            continue
        title = (element.findtext(_TAG_TITLE) or "").strip()
        published = _parse_date(element.findtext(_TAG_DATE))
        items.append(FeedItem(title=title, link=link, published=published))
    return items


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None
