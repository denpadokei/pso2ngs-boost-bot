"""「PSO2の日」のレアドロップブースト期間を検知する。

告知記事 https://pso2.jp/players/news/31542/ の「PSO2の日」ブロックを読み、
毎月の開催日とブースト内容から対象月のイベントを生成する。

この記事は表の1列目が th で td が2列しかないため、汎用の記事パーサー
（article.py）ではイベント化できない。また毎月繰り返し開催されるため、
RSS の新着検知（14日以内）だけでは取りこぼす。そこで専用の情報源として、
実行時点の月と翌月の開催回を組み立てる。
"""

from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

from .fetch import SITE, FetchError
from .models import BoostEvent, event_key
from .periods import Period, Point, normalize
from .tz import JST

if TYPE_CHECKING:  # pragma: no cover
    from .fetch import Fetcher

log = logging.getLogger(__name__)

PSO2_DAY_URL = SITE + "/players/news/31542/"

# ページが読めない／構造が変わったときの既定値（告知記事の記載どおり）。
DEFAULT_START_TIME = (0, 0)
DEFAULT_END_TIME = (23, 59)
DEFAULT_BUFFS_ALL = ("獲得経験値+50%", "レアドロップ倍率+100%")
DEFAULT_BUFFS_PREMIUM = ("レアドロップ倍率+50%", "プリセット能力付与倍率+50%")
DEFAULT_MONTHS = 2

_TIME_RE = re.compile(r"(\d{1,2})\s*:\s*(\d{2})")


@dataclass(frozen=True)
class Window:
    """毎月の開催日と時刻（日付は月をまたいで再利用する）。"""

    start_day: int
    end_day: int
    start_time: tuple[int, int] = DEFAULT_START_TIME
    end_time: tuple[int, int] = DEFAULT_END_TIME


@dataclass
class Discovery:
    """検出結果と、告知ページから実データを読めたかどうか。"""

    events: list[BoostEvent]
    fetched: bool = True  # HTTP 取得に成功したか
    parsed: bool = True  # 開催日・倍率を実際に読み取れたか

    @property
    def ok(self) -> bool:
        """取得も解析も成功した場合だけ True（既定値フォールバックは False）。"""
        return self.fetched and self.parsed


@dataclass(frozen=True)
class Template:
    """告知ページから読み取った、毎月繰り返しの開催情報。"""

    windows: tuple[Window, ...]
    buffs_all: tuple[str, ...] = DEFAULT_BUFFS_ALL
    buffs_premium: tuple[str, ...] = DEFAULT_BUFFS_PREMIUM
    parsed: bool = True

    @classmethod
    def defaults(cls) -> "Template":
        return cls(
            windows=(
                Window(1, 3),
                Window(21, 23),
            ),
            parsed=False,
        )


def _text(element) -> str:
    return normalize(element.get_text(" ", strip=True)) if element else ""


def _list_items(element) -> list[str]:
    if element is None:
        return []
    return [value for value in (_text(li) for li in element.select("li")) if value]


def _pso2day_block(soup: BeautifulSoup):
    """「PSO2の日」本体のブロックを選ぶ（廣告キャンペーン群は除外）。"""
    block = soup.select_one("#cp01")
    if block is not None:
        return block
    for candidate in soup.select("div.template01Block"):
        heading = candidate.find("h2")
        if heading is not None and _text(heading) == "PSO2の日":
            return candidate
    return None


def _time_of(element, default: tuple[int, int]) -> tuple[int, int]:
    match = _TIME_RE.search(_text(element))
    if match is None:
        return default
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    return hour, minute


def _windows(block) -> tuple[Window, ...]:
    windows: list[Window] = []
    for period in block.select(".period"):
        start = period.select_one(".period-start")
        end = period.select_one(".period-end")
        if start is None or end is None:
            continue
        start_day = _text(start.select_one(".day"))
        end_day = _text(end.select_one(".day"))
        if not start_day.isdigit() or not end_day.isdigit():
            continue
        first, last = int(start_day), int(end_day)
        if not (1 <= first <= 31 and 1 <= last <= 31 and first <= last):
            continue
        windows.append(
            Window(
                start_day=first,
                end_day=last,
                start_time=_time_of(start, DEFAULT_START_TIME),
                end_time=_time_of(end, DEFAULT_END_TIME),
            )
        )
    return tuple(windows)


def _buffs(block) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """「ブースト内容」行から全プレイヤー／プレミアムの倍率を拾う。"""
    for row in block.select("tr"):
        header = row.find("th")
        if header is None or "ブースト内容" not in _text(header):
            continue
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        return tuple(_list_items(cells[0])), tuple(_list_items(cells[1]))
    return (), ()


def parse_page(html: str) -> Template:
    """告知 HTML から毎月の開催情報を取り出す。取れない項目は既定値を使う。

    既定値に倒れた項目はログに残し、``parsed=False`` を返す。呼び出し側は
    これを監視（既定値で通知し続けている状態）の検知に使う。
    """
    soup = BeautifulSoup(html, "html.parser")
    block = _pso2day_block(soup)
    if block is None:
        log.warning("PSO2の日のブロックが見つからないため既定値を使います")
        return Template.defaults()

    defaults = Template.defaults()
    windows = _windows(block)
    buffs_all, buffs_premium = _buffs(block)
    parsed = True
    if not windows:
        log.warning("PSO2の日の開催日を読み取れず既定値を使います（ページ構造の変更を確認）")
        windows, parsed = defaults.windows, False
    if not buffs_all:
        log.warning("PSO2の日のブースト内容（全プレイヤー）を読み取れず既定値を使います")
        buffs_all, parsed = DEFAULT_BUFFS_ALL, False
    if not buffs_premium:
        log.warning("PSO2の日のブースト内容（プレミアム）を読み取れず既定値を使います")
        buffs_premium, parsed = DEFAULT_BUFFS_PREMIUM, False

    return Template(
        windows=windows,
        buffs_all=buffs_all,
        buffs_premium=buffs_premium,
        parsed=parsed,
    )


def _months(now: datetime, count: int):
    year, month = now.year, now.month
    for _ in range(max(0, count)):
        yield year, month
        month += 1
        if month > 12:
            month, year = 1, year + 1


def events_from_template(
    template: Template,
    now: datetime,
    months: int = DEFAULT_MONTHS,
    url: str = PSO2_DAY_URL,
) -> list[BoostEvent]:
    """対象月（既定: 当月と翌月）の開催回をイベント化する。"""
    events: list[BoostEvent] = []
    seen: set[str] = set()

    for year, month in _months(now, months):
        last_day = calendar.monthrange(year, month)[1]
        for window in template.windows:
            start_day = min(window.start_day, last_day)
            end_day = min(window.end_day, last_day)
            start = datetime(year, month, start_day, *window.start_time, tzinfo=JST)
            end = datetime(year, month, end_day, *window.end_time, tzinfo=JST)
            if end <= start:
                log.warning("PSO2の日の期間が反転しているためスキップ: %s %s", year, window)
                continue
            label = f"PSO2の日（{start_day}～{end_day}日）"
            raw = (
                f"毎月{start_day}日 {window.start_time[0]}:{window.start_time[1]:02d}"
                f"～毎月{end_day}日 {window.end_time[0]}:{window.end_time[1]:02d}"
            )
            period = Period(
                start=Point(at=start, has_time=True),
                end=Point(at=end, has_time=True),
                raw=raw,
            )
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
                    buffs_all=list(template.buffs_all),
                    buffs_premium=list(template.buffs_premium),
                    source="pso2day",
                )
            )
    return events


def discover_pso2_day(
    fetcher: "Fetcher",
    now: datetime,
    months: int = DEFAULT_MONTHS,
    url: str = PSO2_DAY_URL,
) -> Discovery:
    """告知ページを取得して PSO2の日のイベントを返す。取得失敗時は既定値で生成する。

    ``ok`` が False のときは既定値で生成した状態を表し、監視側へ伝える。
    """
    try:
        html = fetcher.get(url, cache=True)
    except FetchError as exc:
        log.warning("PSO2の日の告知記事を取得できませんでした（既定値で生成）: %s", exc)
        return Discovery(
            events_from_template(Template.defaults(), now, months, url),
            fetched=False,
            parsed=False,
        )

    template = parse_page(html)
    events = events_from_template(template, now, months, url)
    log.info(
        "PSO2の日を %d 件検出: %s",
        len(events),
        ", ".join(event.label for event in events),
    )
    return Discovery(events, parsed=template.parsed)
