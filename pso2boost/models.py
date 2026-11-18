"""通知対象のデータ構造。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .periods import Period


@dataclass
class BoostEvent:
    """1件のブーストキャンペーン。"""

    key: str
    label: str
    period: Period
    url: str | None = None
    article_title: str = ""
    description: str = ""
    quests: list[str] = field(default_factory=list)
    buffs_all: list[str] = field(default_factory=list)
    buffs_premium: list[str] = field(default_factory=list)
    source: str = "schedule"

    @property
    def has_detail(self) -> bool:
        return bool(self.quests or self.buffs_all or self.buffs_premium)

    @property
    def location(self) -> str:
        """Discord のイベント場所欄（100文字制限）に収まる形にする。"""
        if not self.quests:
            return "すべてのクエスト"
        if len(self.quests) <= 3:
            return " / ".join(self.quests)
        return " / ".join(self.quests[:3]) + f" ほか{len(self.quests) - 3}件"


def event_key(label: str, period: Period) -> str:
    """ラベルと期間から安定したキーを作る（期間変更時は別イベント扱い）。"""
    digest = hashlib.sha1(f"{label}|{period.key_part()}".encode("utf-8")).hexdigest()
    return digest[:12]


def merge_events(primary: list[BoostEvent], secondary: list[BoostEvent]) -> list[BoostEvent]:
    """同じキーのイベントを1つにまとめる（primary を優先し、詳細だけ補完）。"""
    merged: dict[str, BoostEvent] = {event.key: event for event in primary}
    for event in secondary:
        existing = merged.get(event.key)
        if existing is None:
            merged[event.key] = event
            continue
        if not existing.has_detail and event.has_detail:
            existing.quests = event.quests
            existing.buffs_all = event.buffs_all
            existing.buffs_premium = event.buffs_premium
            existing.article_title = existing.article_title or event.article_title
            existing.description = existing.description or event.description
        if not existing.url and event.url:
            existing.url = event.url
    return list(merged.values())
