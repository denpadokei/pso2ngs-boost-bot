"""Bot トークンで使う最小限の Discord REST クライアント。

旧 cloud/interactions.py にあったものを、interactions 廃止に伴って
ここへ移した。エラーには HTTP ステータスを status_code として持たせる。
"""

from __future__ import annotations

from typing import Any

import requests

API_BASE = "https://discord.com/api/v10"


class DiscordRestError(RuntimeError):
    """Discord API への確認に失敗した。status_code に HTTP ステータスを持つ。"""

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


class DiscordRest:
    """/setup と週次バッチに必要な最小限の REST クライアント。"""

    def __init__(self, token: str, *, session=None, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bot {token}"})

    def get(self, path: str) -> Any:
        response = self.session.get(API_BASE + path, timeout=self.timeout)
        if response.status_code >= 400:
            raise DiscordRestError(
                f"HTTP {response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
            )
        return response.json()

    def get_channel(self, channel_id: str) -> dict:
        return self.get(f"/channels/{channel_id}")

    def get_guild(self, guild_id: str) -> dict:
        return self.get(f"/guilds/{guild_id}")

    def get_guild_channels(self, guild_id: str) -> list[dict]:
        channels = self.get(f"/guilds/{guild_id}/channels")
        return channels if isinstance(channels, list) else []
