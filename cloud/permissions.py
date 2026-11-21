"""Bot の「選択されたチャンネル」での実効権限を求める共通ヘルパー。

OAuth 設定画面と Interactions の `/setup` で同じ判定を使い、
「呼び出し元チャンネルの権限」を誤って通知先の権限としないようにする。
"""

from __future__ import annotations

import logging
from typing import Any

from .discord import compute_channel_permissions

log = logging.getLogger(__name__)


def bot_roles(discord: Any, guild_id: str) -> tuple[set[str], list[dict]] | None:
    """Bot のロールID集合とギルドのロール一覧（取得できなければ None）。"""
    try:
        me = discord.get_me()
        member = discord.get_guild_member(guild_id, str(me.get("id") or ""))
        roles = discord.get_guild_roles(guild_id)
    except Exception:
        log.warning(
            "ギルド %s の Bot 権限情報を取得できませんでした", guild_id, exc_info=True
        )
        return None
    return {str(role) for role in member.get("roles") or []}, roles


def effective_channel_permissions(
    discord: Any, guild_id: str, channel: dict
) -> int | None:
    """選択チャンネルでの Bot の実効権限。判定不能なら None。"""
    info = bot_roles(discord, guild_id)
    if info is None:
        return None
    role_ids, roles = info
    return compute_channel_permissions(
        guild_id=guild_id, role_ids=role_ids, roles=roles, channel=channel
    )
