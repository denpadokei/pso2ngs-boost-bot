"""期間文字列のパース。

pso2.jp の表記はおおむね次の2種類。

- スケジュール: ``11/11 12:00～11/18 メンテナンス開始まで``
- 記事本文:     ``2026/11/11（水）12:00～2026/11/18（水）メンテナンス開始``

「メンテナンス開始 / 終了」のように時刻が書かれない境界は、実際の時刻を
断定できないので 11:00 JST を仮の境界として持たせ、表示には注記を出す。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

from .tz import JST

if TYPE_CHECKING:  # pragma: no cover
    from .maintenance import MaintenanceCalendar

# NGS の定期メンテナンスは 11:00 開始。境界時刻の仮置きに使う。
MAINTENANCE_HOUR = 11

_DATE_RE = re.compile(
    r"(?:(?P<year>\d{4})/)?(?P<month>\d{1,2})/(?P<day>\d{1,2})"
    r"(?:\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}))?"
)
_SPLIT_RE = re.compile(r"\s*[~〜]\s*")

MAINTENANCE_START_WORDS = ("メンテナンス開始", "メンテ開始")
MAINTENANCE_END_WORDS = ("メンテナンス終了", "メンテ終了")


@dataclass(frozen=True)
class Point:
    """期間の片側（開始または終了）。"""

    at: datetime | None
    note: str = ""
    has_time: bool = False

    def display(self) -> str:
        if self.at is None:
            return self.note or "未定"
        text = f"{self.at:%Y/%m/%d}"
        if self.has_time:
            text += f" {self.at:%H:%M}"
        if self.note:
            text += f" {self.note}"
        return text


@dataclass(frozen=True)
class Period:
    start: Point
    end: Point
    raw: str

    def key_part(self) -> str:
        start = self.start.at.isoformat() if self.start.at else "-"
        end = self.end.at.isoformat() if self.end.at else "-"
        return f"{start}~{end}"

    def display(self) -> str:
        return f"{self.start.display()} ～ {self.end.display()}"

    def is_expired(self, now: datetime, grace_days: int = 1) -> bool:
        if self.end.at is None:
            return False
        return (now - self.end.at).days > grace_days


def normalize(text: str) -> str:
    """全角数字・波ダッシュ・不要な括弧書きをそろえる。"""
    value = unicodedata.normalize("NFKC", text)
    value = value.replace("〜", "~").replace("～", "~").replace("－", "-").replace("—", "-")
    value = re.sub(r"\([^()]*\)", "", value)  # （水）（予定）など
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _parse_point(text: str, default_year: int) -> Point:
    raw = normalize(text)
    if not raw or raw in {"未定", "未定です"}:
        return Point(None, note="未定")

    match = _DATE_RE.search(raw)
    if match is None:
        return Point(None, note=raw.strip(" まで~"))

    year = int(match.group("year")) if match.group("year") else default_year
    month = int(match.group("month"))
    day = int(match.group("day"))
    has_time = match.group("hour") is not None
    hour = int(match.group("hour")) if has_time else 0
    minute = int(match.group("minute")) if match.group("minute") else 0

    tail = normalize(raw[match.end() :]).lstrip("~ ").strip()
    for suffix in ("まで", "迄"):
        if tail.endswith(suffix):
            tail = tail[: -len(suffix)].strip()
    note = tail

    if not has_time:
        if any(word in note for word in MAINTENANCE_END_WORDS):
            hour = MAINTENANCE_HOUR
        elif any(word in note for word in MAINTENANCE_START_WORDS):
            hour = MAINTENANCE_HOUR
        else:
            hour = 0

    try:
        at = datetime(year, month, day, hour, minute, tzinfo=JST)
    except ValueError:
        return Point(None, note=note or raw)
    return Point(at=at, note=note, has_time=has_time)


def parse_period(
    text: str,
    default_year: int,
    now: datetime | None = None,
    calendar: "MaintenanceCalendar | None" = None,
) -> Period:
    """期間文字列を ``Period`` にする。解釈できなければ両端 None。"""
    raw = normalize(text)
    if now is not None and default_year is None:  # pragma: no cover - 保険
        default_year = now.year

    parts = _SPLIT_RE.split(raw, maxsplit=1)
    if len(parts) == 1:
        start = _parse_point(parts[0], default_year)
        if calendar is not None:
            start = calendar.resolve(start)
        return Period(start, Point(None, note="未定"), raw)

    start = _parse_point(parts[0], default_year)
    end = _parse_point(parts[1], default_year)

    # 年またぎ（12/25～1/10 など）
    if start.at and end.at and end.at < start.at:
        end = _parse_point(parts[1], start.at.year + 1)

    # メンテナンス予定表があれば「メンテナンス開始 / 終了」を実時刻に解決
    if calendar is not None:
        start = calendar.resolve(start)
        end = calendar.resolve(end)

    return Period(start=start, end=end, raw=raw)


def with_year(point: Point, year: int) -> Point:
    if point.at is None:
        return point
    return replace(point, at=point.at.replace(year=year))
