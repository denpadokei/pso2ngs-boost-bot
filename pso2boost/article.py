"""告知記事の本文からブーストの詳細（対象クエスト・倍率）を取り出す。

記事によっては表がなかったり、列の順番が違ったりするので、
ヘッダー行の文言から列の意味を判断する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

from .models import BoostEvent, event_key
from .periods import Period, normalize, parse_period

if TYPE_CHECKING:  # pragma: no cover
    from .maintenance import MaintenanceCalendar


@dataclass
class BoostRow:
    label: str
    period: Period
    quests: list[str] = field(default_factory=list)
    buffs_all: list[str] = field(default_factory=list)
    buffs_premium: list[str] = field(default_factory=list)

@dataclass
class BoostBlock:
    heading: str
    period: Period | None
    intro: str = ""
    rows: list[BoostRow] = field(default_factory=list)


@dataclass
class Article:
    url: str
    title: str
    blocks: list[BoostBlock] = field(default_factory=list)
    mentions_boost: bool = False

    def pick_row(self, label: str) -> BoostRow | None:
        target = normalize(label)
        rows = [row for block in self.blocks for row in block.rows]
        for row in rows:
            if normalize(row.label) == target:
                return row
        if len(rows) == 1:
            return rows[0]
        return None

    def pick_block(self, label: str) -> BoostBlock | None:
        target = normalize(label)
        for block in self.blocks:
            if normalize(block.heading) == target:
                return block
        return None


def _text(element) -> str:
    return normalize(element.get_text(" ", strip=True)) if element else ""


def _list_items(element) -> list[str]:
    if element is None:
        return []
    items = [_text(li) for li in element.select("li")]
    values = [item for item in items if item]
    if not values:
        value = _text(element)
        if value:
            values = [value]
    return values


def _row_label(cell) -> str:
    """1列目からイベント名だけ取り出す（期間の ul を除く）。"""
    clone = BeautifulSoup(str(cell), "html.parser")
    for ul in clone.find_all("ul"):
        ul.decompose()
    label = _text(clone)
    if label:
        return label
    return "ブーストキャンペーン"


def _row_period(
    cell, default_year: int, now: datetime, calendar: "MaintenanceCalendar | None" = None
) -> Period:
    date_list = cell.select("ul li")
    raw = " ".join(_text(li) for li in date_list)
    if not raw:
        raw = _text(cell)
    return parse_period(raw.replace("<br />", " "), default_year, now, calendar)


def _column_index(headers: list[str], *, premium: bool) -> int | None:
    for index, header in enumerate(headers):
        if premium and "プレミアム" in header:
            return index
        if not premium and "プレイヤー" in header and "プレミアム" not in header:
            return index
    return None


HIGHLIGHT_HEADINGS = ("ブースト", "強化応援")
_EFFECT_RE = re.compile(r"[+＋]\s*\d+\s*%|\d+\s*%\s*オフ|倍率|成功率|アップ")


def _looks_like_effect(heading: str, rows: list[BoostRow]) -> bool:
    """ブースト以外（装備強化応援など）も効果内容から拾う。"""
    if any(keyword in heading for keyword in HIGHLIGHT_HEADINGS):
        return True
    for row in rows:
        for value in [*row.buffs_all, *row.buffs_premium]:
            if _EFFECT_RE.search(value):
                return True
    return False


def parse_article_html(
    html: str, url: str, now: datetime, calendar: "MaintenanceCalendar | None" = None
) -> Article:
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    og_title = soup.select_one('meta[property="og:title"]')
    if og_title and og_title.get("content"):
        title = normalize(og_title["content"])
    if not title:
        title = _text(soup.find("h1"))

    body_text = _text(soup.select_one("main") or soup)
    article = Article(url=url, title=title, mentions_boost="ブースト" in body_text)

    for block_element in soup.select("div.template01Block"):
        heading_element = block_element.find("h2")
        heading = _text(heading_element)

        period_element = block_element.select_one(".period")
        period = (
            parse_period(_text(period_element), _extract_year(period_element, now), now, calendar)
            if period_element
            else None
        )

        intro_parts = [
            _text(p)
            for p in block_element.find_all("p")
            if p.find_parent("table") is None
            and p.find_parent(class_="period") is None
        ]
        intro = " ".join(part for part in intro_parts if part)

        block = BoostBlock(heading=heading, period=period, intro=intro)
        table = block_element.find("table")
        if table is not None:
            block.rows = _parse_table(table, now, calendar)

        # 見出しが「ブースト」でなくても、効果に倍率・成功率・割引が入っていれば対象
        if not _looks_like_effect(heading, block.rows):
            continue
        article.blocks.append(block)

    return article


def _extract_year(element, now: datetime) -> int:
    for span in element.select(".year"):
        value = _text(span)
        if value.isdigit():
            return int(value)
    return now.year


def _parse_table(
    table, now: datetime, calendar: "MaintenanceCalendar | None" = None
) -> list[BoostRow]:
    header_cells = [_text(th) for th in table.select("tr.point th")]
    index_all = _column_index(header_cells, premium=False)
    index_premium = _column_index(header_cells, premium=True)

    rows: list[BoostRow] = []
    for tr in table.select("tr"):
        if "point" in (tr.get("class") or []):
            continue
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue

        period = _row_period(cells[0], now.year, now, calendar)
        default_year = period.start.at.year if period.start.at else now.year
        if period.start.at is None:
            period = _row_period(cells[0], default_year, now, calendar)

        def buffs(index: int | None, fallback: int) -> list[str]:
            target = index if index is not None and index < len(cells) else fallback
            if target >= len(cells):
                return []
            return _list_items(cells[target])

        rows.append(
            BoostRow(
                label=_row_label(cells[0]),
                period=period,
                quests=_list_items(cells[1]),
                buffs_all=buffs(index_all, 2),
                buffs_premium=buffs(index_premium, 3),
            )
        )
    return rows


def apply_details(event: BoostEvent, article: Article) -> BoostEvent:
    """記事の内容をイベントへ流し込む（取れた項目だけ上書き）。"""
    if article.title:
        event.article_title = article.title

    row = article.pick_row(event.label)
    if row is not None:
        event.quests = row.quests
        event.buffs_all = row.buffs_all
        event.buffs_premium = row.buffs_premium
        if event.period.start.at is None and row.period.start.at is not None:
            event.period = row.period

    block = article.pick_block(event.label)
    if block is None and row is not None:
        block = next((b for b in article.blocks if row in b.rows), None)
    if block is None and len(article.blocks) == 1:
        block = article.blocks[0]
    if block is not None and block.intro:
        event.description = block.intro

    return event


def events_from_article(article: Article) -> list[BoostEvent]:
    """記事の表から、行ごとのブーストイベントを作る。"""
    events: list[BoostEvent] = []
    seen: set[str] = set()
    for block in article.blocks:
        for row in block.rows:
            if row.period.start.at is None and row.period.end.at is None:
                continue
            key = event_key(row.label, row.period)
            if key in seen:
                continue
            seen.add(key)
            events.append(
                BoostEvent(
                    key=key,
                    label=row.label,
                    period=row.period,
                    url=article.url,
                    article_title=article.title,
                    description=block.intro,
                    quests=row.quests,
                    buffs_all=row.buffs_all,
                    buffs_premium=row.buffs_premium,
                    source="rss",
                )
            )
    return events
