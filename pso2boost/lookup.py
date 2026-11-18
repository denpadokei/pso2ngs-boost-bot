"""Bot トークンからサーバー・チャンネルを引くための小さなヘルパー。

招待画面でサーバーは選べるが、その ID はアプリに渡らない。
チャンネルはそもそも招待では選べない。
どちらも Bot トークンで REST から取得できるので、ID のコピペを不要にする。
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"

# 0: テキスト, 5: アナウンス
TEXT_CHANNEL_TYPES = (0, 5)


class DiscordLookupError(RuntimeError):
    """サーバー・チャンネルを特定できなかった。"""


class DiscordLookup:
    def __init__(self, token: str, *, session: requests.Session | None = None, timeout: float = 20.0):
        self.token = (token or "").strip()
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bot {self.token}"})

    def _get(self, path: str):
        response = self.session.get(f"{API}{path}", timeout=self.timeout)
        if response.status_code == 401:
            raise DiscordLookupError("Bot トークンが無効です")
        if response.status_code >= 400:
            raise DiscordLookupError(f"HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def guilds(self) -> list[dict]:
        return self._get("/users/@me/guilds")

    def channels(self, guild_id: str) -> list[dict]:
        return self._get(f"/guilds/{guild_id}/channels")

    def resolve_guild_id(self, preferred: str | None = None) -> str:
        if preferred:
            return str(preferred)

        guilds = self.guilds()
        if not guilds:
            raise DiscordLookupError("Bot が参加しているサーバーがありません。先に招待してください")
        if len(guilds) == 1:
            return str(guilds[0]["id"])

        listing = ", ".join(f"{item.get('name')}({item.get('id')})" for item in guilds)
        raise DiscordLookupError(
            f"サーバーを1つに特定できません。DISCORD_GUILD_ID を指定してください: {listing}"
        )

    def resolve_channel_id(
        self,
        guild_id: str,
        *,
        preferred_id: str | None = None,
        name: str | None = None,
    ) -> str:
        if preferred_id:
            return str(preferred_id)
        if not name:
            return ""

        target = name.lstrip("#").strip().casefold()
        channels = [
            channel
            for channel in self.channels(guild_id)
            if channel.get("type") in TEXT_CHANNEL_TYPES
        ]
        for channel in channels:
            if str(channel.get("name", "")).casefold() == target:
                return str(channel["id"])

        listing = ", ".join(f"#{channel.get('name')}" for channel in channels[:20])
        raise DiscordLookupError(
            f"チャンネル #{name} が見つかりません。投稿先の候補: {listing or 'なし'}"
        )
