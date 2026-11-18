"""Discord のスケジュールイベント（EXTERNAL）を操作する REST クライアント。

Gateway 接続は不要。Bot トークンで REST API を叩くだけなので、
GitHub Actions のような短命なプロセスからでも実行できる。

必要な権限（2026年2月以降）:
- ``CREATE_EVENTS`` (1 << 44) … 作成
- ``MANAGE_EVENTS`` (1 << 33) … 変更・削除
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import requests

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
ENTITY_TYPE_EXTERNAL = 3
PRIVACY_LEVEL_GUILD_ONLY = 2

PERMISSION_MANAGE_EVENTS = 1 << 33
PERMISSION_CREATE_EVENTS = 1 << 44
PERMISSION_SEND_MESSAGES = 1 << 11
PERMISSION_VIEW_CHANNEL = 1 << 10
REQUIRED_PERMISSIONS = PERMISSION_CREATE_EVENTS | PERMISSION_MANAGE_EVENTS
# Bot から投稿もする場合の推奨セット
RECOMMENDED_PERMISSIONS = (
    REQUIRED_PERMISSIONS | PERMISSION_SEND_MESSAGES | PERMISSION_VIEW_CHANNEL
)


class DiscordApiError(RuntimeError):
    """Discord API がエラーを返した。"""


def invite_url(client_id: str, permissions: int = RECOMMENDED_PERMISSIONS) -> str:
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={client_id}&scope=bot&permissions={permissions}"
    )


class ScheduledEventsApi:
    def __init__(
        self,
        token: str,
        guild_id: str,
        *,
        base_url: str = API_BASE,
        timeout: float = 20.0,
        session: requests.Session | None = None,
    ) -> None:
        self.token = (token or "").strip()
        self.guild_id = str(guild_id or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bot {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "pso2ngs-boost-bot/0.1 (+https://pso2.jp/)",
            }
        )

    @property
    def configured(self) -> bool:
        return bool(self.token and self.guild_id)

    def event_url(self, event_id: str) -> str:
        return f"https://discord.com/events/{self.guild_id}/{event_id}"

    # ------------------------------------------------------------------ CRUD
    def create(
        self,
        *,
        name: str,
        description: str,
        start: datetime,
        end: datetime,
        location: str,
    ) -> str:
        payload = _payload(name, description, start, end, location)
        data = self._request("POST", self._collection_url(), payload)
        event_id = (data or {}).get("id")
        if not event_id:
            raise DiscordApiError("作成レスポンスに id がありません")
        return str(event_id)

    def update(
        self,
        event_id: str,
        *,
        name: str,
        description: str,
        start: datetime,
        end: datetime,
        location: str,
    ) -> None:
        payload = _payload(name, description, start, end, location)
        self._request("PATCH", self._item_url(event_id), payload)

    def delete(self, event_id: str) -> None:
        self._request("DELETE", self._item_url(event_id))

    def list(self) -> list[dict[str, Any]]:
        data = self._request("GET", self._collection_url())
        return data if isinstance(data, list) else []

    # --------------------------------------------------------------- request
    def _collection_url(self) -> str:
        return f"{self.base_url}/guilds/{self.guild_id}/scheduled-events"

    def _item_url(self, event_id: str) -> str:
        return f"{self._collection_url()}/{event_id}"

    def _request(
        self, method: str, url: str, payload: dict | None = None
    ) -> Any:
        if not self.configured:
            raise DiscordApiError("DISCORD_BOT_TOKEN / DISCORD_GUILD_ID が未設定です")

        last_error = ""
        for attempt in range(1, 4):
            response = self.session.request(
                method, url, json=payload, timeout=self.timeout
            )
            if response.status_code == 429:
                retry_after = 1.0
                try:
                    retry_after = float(response.json().get("retry_after", 1.0))
                except ValueError:
                    pass
                time.sleep(min(retry_after, 10.0))
                last_error = "rate limited"
                continue
            if response.status_code >= 500 and attempt < 3:
                time.sleep(2**attempt)
                last_error = f"HTTP {response.status_code}"
                continue
            if response.status_code >= 400:
                raise DiscordApiError(
                    f"HTTP {response.status_code}: {response.text[:400]}"
                )
            if response.status_code == 204 or not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return None

        raise DiscordApiError(f"リクエストに失敗しました ({last_error})")


def _payload(
    name: str, description: str, start: datetime, end: datetime, location: str
) -> dict:
    payload: dict[str, Any] = {
        "name": name[:100],
        "scheduled_start_time": start.isoformat(),
        "scheduled_end_time": end.isoformat(),
        "privacy_level": PRIVACY_LEVEL_GUILD_ONLY,
        "entity_type": ENTITY_TYPE_EXTERNAL,
        "entity_metadata": {"location": (location or "PSO2NGS")[:100]},
    }
    if description:
        payload["description"] = description[:1000]
    return payload
