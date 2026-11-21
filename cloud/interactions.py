"""Discord Interactions の deferred 処理（Cloudflare Worker の後段）。

Worker が Discord へ type 5（「考え中…」）を即座に返し、生ボディと署名
ヘッダーをここへ転送してくる。このモジュールは:

1. 署名を再検証する（共有シークレットを置かずに送信元を保証する）
2. ``/setup`` の実処理（権限・チャンネル検証と Table 保存）を行う
3. ``PATCH /webhooks/{app_id}/{token}/messages/@original`` で
   「考え中…」を結果本文へ差し替える

Azure 側はスケールゼロでも構わない。3 秒制約は Worker 側で満たしている。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

import requests

from pso2boost.lookup import TEXT_CHANNEL_TYPES
from pso2boost.tz import now_jst

from .config import CloudConfig
from .discord import DiscordRest, DiscordRestError, can_send_messages
from .storage import GuildConfig, TableStore

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"

INTERACTION_APPLICATION_COMMAND = 2

MANAGE_GUILD = 1 << 5


class SignatureError(ValueError):
    """署名が不正（転送元を信頼できない）。"""


def verify_signature(
    public_key_hex: str, timestamp: str, body: bytes, signature_hex: str
) -> bool:
    """Discord 流の Ed25519 署名を検証する。失敗は例外ではなく False。"""
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


def edit_original_response(
    application_id: str,
    token: str,
    content: str,
    *,
    session: Any | None = None,
    timeout: float = 20.0,
) -> bool:
    """deferred メッセージを結果本文へ差し替える。

    type 5 で始まったメッセージは ephemeral が引き継がれるので、
    ここで flags を送る必要はない。トークンは発行から 15 分有効。
    """
    if not application_id or not token:
        log.error("interaction の差し替えに必要な ID / トークンがありません")
        return False
    session = session or requests
    url = f"{API_BASE}/webhooks/{application_id}/{token}/messages/@original"
    try:
        response = session.patch(
            url, json={"content": content[:2000]}, timeout=timeout
        )
    except requests.RequestException as exc:
        log.exception("interaction の差し替えに失敗: %s", exc)
        return False
    if response.status_code >= 400:
        log.error("interaction の差し替えに失敗: HTTP %s", response.status_code)
        return False
    return True


def _permission_names(bits: int) -> list[str]:
    names = [
        (1 << 44, "イベントの作成"),
        (1 << 33, "イベントの管理"),
        (1 << 11, "メッセージを送信"),
        (1 << 10, "チャンネルを見る"),
    ]
    return [name for mask, name in names if bits & mask]


def _odd_permissions_message(missing: int) -> str:
    names = "、".join(_permission_names(missing))
    return f"\n⚠️ Bot に権限が足りません: {names}（招待し直すと直ります）"


def handle_application_command(
    payload: dict,
    *,
    config: CloudConfig,
    store: TableStore | None = None,
    discord: DiscordRest | None = None,
) -> str:
    """``/setup`` を処理して、Discord に表示する本文を返す。"""
    data = payload.get("data") or {}
    if data.get("name") != "setup":
        log.warning("未知のコマンドです: %s", data.get("name"))
        return "⚠️ 未対応のコマンドです。"

    guild_id = str(payload.get("guild_id") or "")
    member = payload.get("member") or {}
    try:
        member_permissions = int(str(member.get("permissions") or "0"))
    except ValueError:
        member_permissions = 0

    if not guild_id or not (member_permissions & MANAGE_GUILD):
        return "❌ このコマンドには「サーバーを管理」（MANAGE_GUILD）権限が必要です。"

    options = {
        str(option.get("name")): option.get("value")
        for option in data.get("options") or []
    }
    channel_value = str(options.get("channel") or "").strip()
    if not channel_value:
        return "⚠️ 通知先のチャンネルを指定してください。"

    discord = discord or DiscordRest(config.bot_token)
    try:
        channel = discord.get_channel(channel_value)
    except DiscordRestError as exc:
        return f"⚠️ チャンネルを確認できませんでした: {exc}"

    if str(channel.get("guild_id") or "") != guild_id:
        return "⚠️ このサーバー内のチャンネルを指定してください。"
    if channel.get("type") not in TEXT_CHANNEL_TYPES:
        return "⚠️ 通知先にはテキストチャンネル（またはアナウンスチャンネル）を指定してください。"

    # インタラクションの app_permissions は Bot の実効権限。OAuth 設定画面と
    # 同じ can_send_messages で判定し、投稿権限なしの 403 を事前に警告する。
    try:
        app_permissions = int(str(payload.get("app_permissions") or "0"))
    except ValueError:
        app_permissions = 0
    permissions_ok = can_send_messages(app_permissions)

    store = store or TableStore(config.storage_connection)
    now = now_jst()
    try:
        store.ensure_tables()
        existing = {guild.guild_id: guild for guild in store.list_guilds()}.get(
            guild_id
        )
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
    except Exception as exc:
        log.exception("guilds テーブルの更新に失敗")
        return f"⚠️ 設定を保存できませんでした: {exc}"

    log.info("サーバー %s の通知先を設定: チャンネル %s", guild_id, guild.channel_id)
    message = (
        f"✅ 通知先を #{channel.get('name') or guild.channel_id} に設定しました\n"
        "次回実行: 毎週水曜 13:00 (JST)"
    )
    if not permissions_ok:
        message += _odd_permissions_message(
            (1 << 10 | 1 << 11) & ~app_permissions
        )
    return message


def process_deferred(
    *,
    body: bytes,
    headers: Mapping[str, str],
    config: CloudConfig,
    store: TableStore | None = None,
    discord: DiscordRest | None = None,
) -> tuple[str, str]:
    """署名検証と /setup 処理を行い ``(token, 表示する本文)`` を返す。

    署名不正のみ SignatureError を投げる。それ以外の失敗は本文として
    返すので、呼び出し側は必ず Discord へ PATCH できる。
    """
    if not config.public_key:
        raise RuntimeError("DISCORD_PUBLIC_KEY が未設定です")

    signature = _header(headers, "X-Signature-Ed25519")
    timestamp = _header(headers, "X-Signature-Timestamp")
    if not verify_signature(config.public_key, timestamp or "", body, signature or ""):
        raise SignatureError("invalid request signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "", "⚠️ リクエストを読み取れませんでした。"

    token = str(payload.get("token") or "")
    if payload.get("type") != INTERACTION_APPLICATION_COMMAND:
        return token, "⚠️ 未対応のインタラクションです。"

    try:
        message = handle_application_command(
            payload, config=config, store=store, discord=discord
        )
    except Exception as exc:
        log.exception("deferred interaction の処理に失敗")
        message = f"⚠️ 設定処理に失敗しました: {exc}"
    return token, message
