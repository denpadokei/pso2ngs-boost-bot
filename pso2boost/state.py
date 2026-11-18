"""通知済み状態の保存（JSON）。

GitHub Actions のように毎回プロセスが終了する環境でも動くよう、
ファイル1枚に「見たイベント」と「送った通知」を記録する。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from .models import BoostEvent
from .tz import JST

log = logging.getLogger(__name__)

STATE_VERSION = 1


class State:
    def __init__(
        self,
        path: str | Path | None,
        now: datetime,
        *,
        data: dict | None = None,
    ) -> None:
        self.path = Path(path) if path else None
        self.now = now
        self._data: dict = {
            "version": STATE_VERSION,
            "seen": {},
            "sent": {},
            "rss": {},
            "discord": {},
        }
        if data is not None:
            self._apply(data)
        else:
            self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("状態ファイルを読めませんでした (%s): %s", self.path, exc)
            return
        self._apply(loaded)

    def _apply(self, loaded: dict) -> None:
        """辞書から内部状態を復元（不正な内容は無視する）。"""
        if not isinstance(loaded, dict):
            return
        self._data["seen"] = dict(loaded.get("seen") or {})
        self._data["sent"] = dict(loaded.get("sent") or {})
        self._data["rss"] = dict(loaded.get("rss") or {})
        self._data["discord"] = dict(loaded.get("discord") or {})

    def snapshot(self) -> dict:
        """内部状態の深いコピー（シリアライズ用）。"""
        return json.loads(json.dumps(self._data, ensure_ascii=False))

    # ------------------------------------------------------------------ seen
    def is_seen(self, key: str) -> bool:
        return key in self._data["seen"]

    def first_seen(self, key: str) -> datetime | None:
        entry = self._data["seen"].get(key)
        if not entry:
            return None
        try:
            return datetime.fromisoformat(entry["first_seen"])
        except (KeyError, TypeError, ValueError):
            return None

    def mark_seen(self, event: BoostEvent) -> None:
        entry = self._data["seen"].get(event.key)
        if entry:
            entry["last_seen"] = self.now.isoformat()
            entry["label"] = event.label
            entry["period"] = event.period.display()
            return
        self._data["seen"][event.key] = {
            "label": event.label,
            "period": event.period.display(),
            "url": event.url,
            "first_seen": self.now.isoformat(),
            "last_seen": self.now.isoformat(),
        }

    # ------------------------------------------------------------------ sent
    def was_sent(self, kind: str, key: str) -> bool:
        return f"{kind}:{key}" in self._data["sent"]

    def mark_sent(self, kind: str, key: str) -> None:
        self._data["sent"][f"{kind}:{key}"] = self.now.isoformat()

    # ------------------------------------------------------------------- rss
    def rss_seen(self, url: str) -> bool:
        return url in self._data["rss"]

    def mark_rss(self, url: str) -> None:
        self._data["rss"][url] = self.now.isoformat()

    # --------------------------------------------------------------- discord
    def discord_event(self, key: str) -> dict | None:
        entry = self._data["discord"].get(key)
        return dict(entry) if isinstance(entry, dict) else None

    def set_discord_event(self, key: str, entry: dict) -> None:
        payload = dict(entry)
        payload["updated"] = self.now.isoformat()
        self._data["discord"][key] = payload

    def remove_discord_event(self, key: str) -> None:
        self._data["discord"].pop(key, None)

    def discord_events(self) -> dict[str, dict]:
        return dict(self._data["discord"])

    # ---------------------------------------------------------------- persist
    def prune(self, days: int = 180) -> None:
        limit = self.now - timedelta(days=days)

        def fresh(value: datetime | None) -> bool:
            return value is not None and value >= limit

        def parse(value):
            try:
                parsed = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)

        self._data["seen"] = {
            key: entry
            for key, entry in self._data["seen"].items()
            if fresh(parse(entry.get("last_seen")))
        }
        self._data["sent"] = {
            key: value for key, value in self._data["sent"].items() if fresh(parse(value))
        }
        self._data["rss"] = {
            key: value for key, value in self._data["rss"].items() if fresh(parse(value))
        }
        self._data["discord"] = {
            key: entry
            for key, entry in self._data["discord"].items()
            if isinstance(entry, dict) and fresh(parse(entry.get("updated")))
        }

    def save(self) -> None:
        if self.path is None:
            log.warning("状態の保存先が未指定のため、保存しません")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2) + "\n"
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, self.path)

    def __len__(self) -> int:
        return len(self._data["seen"])
