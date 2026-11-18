"""Bot トークンで使う最小限の Discord REST クライアント。

旧 cloud/interactions.py にあったものを、interactions 廃止に伴って
ここへ移した。エラーには HTTP ステータスを status_code として持たせる。
"""

from __future__ import annotations

from typing import Any

import requests

API_BASE = "https://discord.com/api/v10"

PERMISSION_ADMINISTRATOR = 1 << 3
PERMISSION_VIEW_CHANNEL = 1 << 10
PERMISSION_SEND_MESSAGES = 1 << 11


def compute_channel_permissions(
    *, guild_id: str, role_ids: set[str], roles: list[dict], channel: dict
) -> int:
    """Bot のチャンネル実効権限を計算する（Discord の権限解決と同じ手順）。

    @everyone の権限 + Bot のロール → チャンネルの @everyone 上書き →
    ロール上書き。Administrator はすべて許可。
    """
    everyone = next((r for r in roles if str(r.get("id")) == str(guild_id)), None)
    permissions = int(str((everyone or {}).get("permissions") or "0"))
    for role in roles:
        if str(role.get("id")) in role_ids:
            permissions |= int(str(role.get("permissions") or "0"))
    if permissions & PERMISSION_ADMINISTRATOR:
        return permissions

    overwrites = channel.get("permission_overwrites") or []
    for overwrite in overwrites:
        if str(overwrite.get("id")) == str(guild_id):
            permissions = (permissions & ~int(str(overwrite.get("deny") or 0))) | int(
                str(overwrite.get("allow") or 0)
            )
    role_allow = role_deny = 0
    for overwrite in overwrites:
        if str(overwrite.get("id")) in role_ids and overwrite.get("type") == 0:
            role_allow |= int(str(overwrite.get("allow") or 0))
            role_deny |= int(str(overwrite.get("deny") or 0))
    return (permissions & ~role_deny) | role_allow


def can_send_messages(permissions: int) -> bool:
    if permissions & PERMISSION_ADMINISTRATOR:
        return True
    return bool(permissions & PERMISSION_VIEW_CHANNEL) and bool(
        permissions & PERMISSION_SEND_MESSAGES
    )


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

    def get_guild_roles(self, guild_id: str) -> list[dict]:
        roles = self.get(f"/guilds/{guild_id}/roles")
        return roles if isinstance(roles, list) else []

    def get_guild_member(self, guild_id: str, member_id: str) -> dict:
        return self.get(f"/guilds/{guild_id}/members/{member_id}")

    def get_me(self) -> dict:
        return self.get("/users/@me")
