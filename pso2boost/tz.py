"""日本時間（JST）の扱い。

Windows には tzdata が入っていないことがあるため、zoneinfo が使えなければ
固定オフセット +09:00 にフォールバックする（日本に夏時間はないので安全）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:  # pragma: no cover - 環境依存
    from zoneinfo import ZoneInfo

    JST = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover - tzdata 未導入の Windows など
    JST = timezone(timedelta(hours=9), "JST")


def now_jst() -> datetime:
    return datetime.now(JST)


def to_jst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=JST)
    return value.astimezone(JST)
