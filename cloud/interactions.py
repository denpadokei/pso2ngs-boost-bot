"""Discord の HTTP Interactions（PING / ``/setup``）。

Gateway への常駐接続は不要。Discord が Function App の URL へ POST する。

- 毎回 Ed25519 署名を検証する（失敗は 401）
- PING（type 1）には ``{"type": 1}``
- ``/setup``: MANAGE_GUILD を確認し、チャンネルを検証して Table へ保存
  - 3秒以内に ephemeral 応答を返す（同期的に1回 API を呼ぶだけ）

このモジュールは azure.functions に依存しない（単体テスト可能）。
HTTP の入口は function_app.py が担う。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

import requests

from pso2boost.discord_events import RECOMMENDED_PERMISSIONS
from pso2boost.lookup import TEXT_CHANNEL_TYPES
from pso2boost.tz import now_jst

from .config import CloudConfig
from .storage import GuildConfig, TableStore

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"

INTERACTION_PING = 1
INTERACTION_APPLICATION_COMMAND = 2

MANAGE_GUILD = 1 << 5
EPHEMERAL_FLAG = 1 << 6


class DiscordRestError(RuntimeError):
    """Discord API への確認に失敗した。"""


class DiscordRest:
    """/setup に必要な最小限の REST クライアント。"""

    def __init__(self, token: str, *, session=None, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bot {token}"})

    def get(self, path: str) -> Any:
        response = self.session.get(API_BASE + path, timeout=self.timeout)
        if response.status_code >= 400:
            raise DiscordRestError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.json()

    def get_channel(self, channel_id: str) -> dict:
        return self.get(f"/channels/{channel_id}")

    def get_guild(self, guild_id: str) -> dict:
        return self.get(f"/guilds/{guild_id}")


# --------------------------------------------------------------------------
# 署名検証
# --------------------------------------------------------------------------
def verify_signature(
    public_key_hex: str, timestamp: str, body: bytes, signature_hex: str
) -> bool:
    """Discord 流の Ed25519 署名検証。失敗は例外ではなく False。"""
    if not public_key_hex or not timestamp or not signature_hex:
        return False
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey

    try:
        verify_key = VerifyKey(bytes.fromhex(public_key_hex))
        verify_key.verify(
            timestamp.encode("utf-8") + body, bytes.fromhex(signature_hex)
        )
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return None


# --------------------------------------------------------------------------
# 応答作成
# --------------------------------------------------------------------------
def ephemeral(content: str) -> dict:
    """3秒以内に返す即時応答（本人にだけ見える）。"""
    return {"type": 4, "data": {"flags": EPHEMERAL_FLAG, "content": content}}


def _permission_names(bits: int) -> list[str]:
    names = [
        (1 << 44, "イベントの作成"),
        (1 << 33, "イベントの管理"),
        (1 << 11, "メッセージを送信"),
        (1 << 10, "チャンネルを見る"),
    ]
    return [name for mask, name in names if bits & mask]


# --------------------------------------------------------------------------
# インタラクション処理
# --------------------------------------------------------------------------
def handle_interaction(
    *,
    body: bytes,
    headers: Mapping[str, str],
    config: CloudConfig,
    store: TableStore | None = None,
    discord: DiscordRest | None = None,
) -> tuple[int, dict]:
    """署名検証してインタラクションを処理する。

    戻り値は (HTTP ステータス, JSON 本文)。
    """
    if not config.public_key:
        log.error("DISCORD_PUBLIC_KEY が未設定です")
        return 503, {"error": "public key is not configured"}

    signature = _header(headers, "X-Signature-Ed25519")
    timestamp = _header(headers, "X-Signature-Timestamp")
    if not verify_signature(config.public_key, timestamp or "", body, signature or ""):
        return 401, {"error": "invalid request signature"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return 400, {"error": "invalid json"}

    kind = payload.get("type")
    if kind == INTERACTION_PING:
        return 200, {"type": 1}
    if kind == INTERACTION_APPLICATION_COMMAND:
        return handle_application_command(
            payload, config=config, store=store, discord=discord
        )

    # 対象外のインタラクション（設計: スラッシュコマンド以外はやらない）
    log.warning("未対応のインタラクションです: type=%s", kind)
    return 200, {}


def handle_application_command(
    payload: dict,
    *,
    config: CloudConfig,
    store: TableStore | None = None,
    discord: DiscordRest | None = None,
) -> tuple[int, dict]:
    data = payload.get("data") or {}
    if data.get("name") != "setup":
        log.warning("未知のコマンドです: %s", data.get("name"))
        return 400, {"error": "unknown command"}

    guild_id = str(payload.get("guild_id") or "")
    member = payload.get("member") or {}
    try:
        member_permissions = int(str(member.get("permissions") or "0"))
    except ValueError:
        member_permissions = 0

    if not guild_id or not (member_permissions & MANAGE_GUILD):
        return 200, ephemeral(
            "❌ このコマンドには「サーバーを管理」（MANAGE_GUILD）権限が必要です。"
        )

    options = {
        str(option.get("name")): option.get("value")
        for option in data.get("options") or []
    }
    channel_value = str(options.get("channel") or "").strip()
    if not channel_value:
        return 200, ephemeral("⚠️ 通知先のチャンネルを指定してください。")

    discord = discord or DiscordRest(config.bot_token)
    try:
        channel = discord.get_channel(channel_value)
    except DiscordRestError as exc:
        return 200, ephemeral(f"⚠️ チャンネルを確認できませんでした: {exc}")

    if str(channel.get("guild_id") or "") != guild_id:
        return 200, ephemeral("⚠️ このサーバー内のチャンネルを指定してください。")
    if channel.get("type") not in TEXT_CHANNEL_TYPES:
        return 200, ephemeral(
            "⚠️ 通知先にはテキストチャンネル（またはアナウンスチャンネル）を指定してください。"
        )

    # Bot 自身の権限はインタラクションの app_permissions から判定する
    try:
        app_permissions = int(str(payload.get("app_permissions") or "0"))
    except ValueError:
        app_permissions = 0
    missing = RECOMMENDED_PERMISSIONS & ~app_permissions
    permissions_ok = missing == 0

    store = store or TableStore(config.storage_connection)
    existing = {
        guild.guild_id: guild for guild in store.list_guilds()
    }.get(guild_id)
    now = now_jst()
    guild = GuildConfig(
        guild_id=guild_id,
        channel_id=str(channel.get("id") or channel_value),
        mention_role_id=existing.mention_role_id if existing else "",
        enabled=True,
        permissions_ok=permissions_ok,
        added_at=existing.added_at if existing else now.isoformat(),
        last_run_at=existing.last_run_at if existing else "",
        last_error="",
    )
    store.upsert_guild(guild)
    log.info("サーバー %s の通知先を設定: チャンネル %s", guild_id, guild.channel_id)

    message = (
        f"✅ 通知先を #{channel.get('name')} に設定しました\n"
        "次回実行: 毎週水曜 13:00 (JST)"
    )
    if not permissions_ok:
        names = "、".join(_permission_names(missing))
        message += f"\n⚠️ Bot に権限が足りません: {names}（招待し直すと直ります）"
    return 200, ephemeral(message)
