"""イベント一覧を Table Storage に置ける JSON へ変換する。

``/sync`` が複数サーバーから呼ばれても pso2.jp への取得を1回に抑えるため、
直近の取得結果を共有スナップショットとして保存・再利用する。
"""

from __future__ import annotations

from datetime import datetime

from pso2boost.models import BoostEvent
from pso2boost.periods import Period, Point


def _point(value: Point) -> dict:
    return {
        "at": value.at.isoformat() if value.at is not None else None,
        "note": value.note,
        "has_time": value.has_time,
    }


def _read_point(value: dict) -> Point:
    raw_at = value.get("at")
    at = datetime.fromisoformat(raw_at) if raw_at else None
    return Point(
        at=at,
        note=str(value.get("note") or ""),
        has_time=bool(value.get("has_time")),
    )


def encode_events(events: list[BoostEvent]) -> dict:
    return {
        "events": [
            {
                "key": event.key,
                "label": event.label,
                "url": event.url,
                "article_title": event.article_title,
                "description": event.description,
                "quests": event.quests,
                "buffs_all": event.buffs_all,
                "buffs_premium": event.buffs_premium,
                "source": event.source,
                "period": {
                    "start": _point(event.period.start),
                    "end": _point(event.period.end),
                    "raw": event.period.raw,
                },
            }
            for event in events
        ]
    }


def decode_events(payload: dict) -> list[BoostEvent]:
    result: list[BoostEvent] = []
    for item in payload.get("events", []):
        period_data = item.get("period") or {}
        period = Period(
            start=_read_point(period_data.get("start") or {}),
            end=_read_point(period_data.get("end") or {}),
            raw=str(period_data.get("raw") or ""),
        )
        result.append(
            BoostEvent(
                key=str(item.get("key") or ""),
                label=str(item.get("label") or ""),
                period=period,
                url=item.get("url"),
                article_title=str(item.get("article_title") or ""),
                description=str(item.get("description") or ""),
                quests=list(item.get("quests") or []),
                buffs_all=list(item.get("buffs_all") or []),
                buffs_premium=list(item.get("buffs_premium") or []),
                source=str(item.get("source") or "schedule"),
            )
        )
    return result
