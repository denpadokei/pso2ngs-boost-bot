"""Discord Interactions の deferred 処理（Cloudflare Worker の後段）。

Worker が Discord へ type 5（「考え中…」）を即座に返し、生ボディと署名
ヘッダーをここへ転送してくる。このモジュールは:

1. 署名を再検証する（共有シークレットを置かずに送信元を保証する）
2. ``/setup``（通知先設定）と ``/sync``（手動同期）の実処理を行う
3. ``PATCH /webhooks/{app_id}/{token}/messages/@original`` で
   「考え中…」を結果本文へ差し替える

Azure 側はスケールゼロでも構わない。3 秒制約は Worker 側で満たしている。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Mapping

import requests

from pso2boost.lookup import TEXT_CHANNEL_TYPES
from pso2boost.tz import now_jst

from .config import CloudConfig
from .discord import DiscordRest, DiscordRestError, can_send_messages
from .permissions import effective_channel_permissions
from .storage import GuildConfig, TableStore

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"

INTERACTION_APPLICATION_COMMAND = 2

MANAGE_GUILD = 1 << 5


class SignatureError(ValueError):
    """署名が不正（転送元を信頼できない）。"""


def is_valid_public_key(public_key_hex: str) -> bool:
    """公開鍵が 32 バイトの hex か（health での設定確認用）。"""
    if not public_key_hex:
        return False
    try:
        return len(bytes.fromhex(public_key_hex)) == 32
    except (ValueError, TypeError):
        return False


def verify_signature(
    public_key_hex: str, timestamp: str, body: bytes, signature_hex: str
) -> bool:
    """Discord 流の Ed25519 署名を検証する。失敗は例外ではなく False。"""
    if not public_key_hex or not timestamp or not signature_hex:
        return False
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey

    try:
        key_bytes = bytes.fromhex(public_key_hex)
    except (ValueError, TypeError):
        log.error("DISCORD_PUBLIC_KEY の hex 形式が不正です")
        return False
    try:
        signature_bytes = bytes.fromhex(signature_hex)
    except (ValueError, TypeError):
        # 公開エンドポイントへ直接送られた不正入力なので warning に留める
        log.warning("署名の hex 形式が不正です")
        return False
    try:
        verify_key = VerifyKey(key_bytes)
        verify_key.verify(timestamp.encode("utf-8") + body, signature_bytes)
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
        # 例外文にはトークン付き URL が含まれるため、種別と末尾だけ残す
        log.error(
            "interaction PATCH 失敗: %s (token ..%s)",
            type(exc).__name__,
            token[-8:],
        )
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


def _handle_sync(
    payload: dict,
    *,
    config: CloudConfig,
    store: TableStore | None = None,
    sync_run=None,
) -> str:
    """``/sync`` を処理して、Discord に表示する本文を返す。"""
    guild_id = str(payload.get("guild_id") or "")
    member = payload.get("member") or {}
    try:
        member_permissions = int(str(member.get("permissions") or "0"))
    except ValueError:
        member_permissions = 0
    if not guild_id or not (member_permissions & MANAGE_GUILD):
        return "❌ このコマンドには「サーバーを管理」（MANAGE_GUILD）権限が必要です。"

    store = store or TableStore(config.storage_connection)
    try:
        store.ensure_tables()
        guild = next(
            (g for g in store.list_guilds() if g.guild_id == guild_id), None
        )
    except Exception:
        log.exception("guilds テーブルの読み出しに失敗")
        return "⚠️ 設定を確認できませんでした。時間をおいてお試しください。"
    if guild is None or not guild.enabled:
        return "⚠️ このサーバーは未設定です。先に /setup で通知先を指定してください。"

    if sync_run is None:
        from dataclasses import replace

        from .runner import run_weekly

        # Worker の転送タイムアウト（20秒）内に終わるよう取得間隔を詰める
        fast_config = replace(config, delay=0.0)
        sync_run = lambda: run_weekly(fast_config, store=store, guild_ids=[guild_id])

    try:
        summary = sync_run()
    except Exception:
        log.exception("手動同期に失敗")
        return "⚠️ 同期に失敗しました。時間をおいてもう一度お試しください。"

    status = getattr(summary, "status", "ok")
    if status == "skipped":
        return "⏭️ 同期をスキップしました（別の実行中、または対象がありません）。"
    headline = "✅ 同期が完了しました" if status == "ok" else "⚠️ 同期に問題がありました"
    message = (
        f"{headline}\n"
        f"通知 {summary.notified} / 作成 {summary.created} / "
        f"更新 {summary.updated} / 削除 {summary.deleted}"
    )
    if getattr(summary, "failed", 0):
        message += f" / 失敗 {summary.failed}"
    for action in list(getattr(summary, "actions", []))[:3]:
        message += f"\n・{str(action)[:120]}"
    return message


def handle_application_command(
    payload: dict,
    *,
    config: CloudConfig,
    store: TableStore | None = None,
    discord: DiscordRest | None = None,
    sync_run=None,
) -> str:
    """``/setup`` / ``/sync`` を処理して、Discord に表示する本文を返す。"""
    data = payload.get("data") or {}
    name = data.get("name")
    if name == "sync":
        return _handle_sync(payload, config=config, store=store, sync_run=sync_run)
    if name != "setup":
        log.warning("未知のコマンドです: %s", name)
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
        # 例外本文には API のレスポンスが含まれることがあるため送らない
        detail = f" (HTTP {exc.status_code})" if exc.status_code else ""
        return f"⚠️ チャンネルを確認できませんでした{detail}。"

    if str(channel.get("guild_id") or "") != guild_id:
        return "⚠️ このサーバー内のチャンネルを指定してください。"
    if channel.get("type") not in TEXT_CHANNEL_TYPES:
        return "⚠️ 通知先にはテキストチャンネル（またはアナウンスチャンネル）を指定してください。"

    # app_permissions は「コマンドを呼んだ場所」の権限であって通知先ではない。
    # OAuth フローと同じヘルパーで、選択されたチャンネルの実効権限を求める。
    effective = effective_channel_permissions(discord, guild_id, channel)
    permission_unknown = effective is None
    # 判定できなかったことを「確認済み」と記録しない
    permissions_ok = can_send_messages(effective) if effective is not None else False

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
    except Exception:
        # ユーザー向けには機微情報を出さないが、トレースバックは server 側へ残す
        log.exception("guilds テーブルの更新に失敗")
        return "⚠️ 設定を保存できませんでした。時間をおいてもう一度お試しください。"

    log.info("サーバー %s の通知先を設定: チャンネル %s", guild_id, guild.channel_id)
    message = (
        f"✅ 通知先を #{channel.get('name') or guild.channel_id} に設定しました\n"
        "次回実行: 毎週水曜 13:00 (JST)"
    )
    if permission_unknown:
        message += "\n⚠️ Bot の権限を確認できませんでした。投稿されない場合は招待し直してください。"
    else:
        missing = ((1 << 10) | (1 << 11)) & ~effective
        if not permissions_ok and missing:
            message += _odd_permissions_message(missing)
    return message


def process_deferred(
    *,
    body: bytes,
    headers: Mapping[str, str],
    config: CloudConfig,
    store: TableStore | None = None,
    discord: DiscordRest | None = None,
    sync_run=None,
) -> tuple[str, str]:
    """署名検証と /setup / /sync 処理を行い ``(token, 表示する本文)`` を返す。

    署名不正のみ SignatureError を投げる。それ以外の失敗は本文として
    返すので、呼び出し側は必ず Discord へ PATCH できる。
    """
    if not config.public_key:
        raise RuntimeError("DISCORD_PUBLIC_KEY が未設定です")

    signature = _header(headers, "X-Signature-Ed25519")
    timestamp = _header(headers, "X-Signature-Timestamp")
    if not verify_signature(config.public_key, timestamp or "", body, signature or ""):
        raise SignatureError("invalid request signature")

    # Worker を経由しない直接呼び出しもあるので、Azure 側でも鮮度を確認する
    try:
        issued_at = int(timestamp or "0")
    except (TypeError, ValueError):
        issued_at = 0
    if abs(time.time() - issued_at) > 300:
        log.warning("stale signature rejected: %s", timestamp)
        raise SignatureError("stale request signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "", "⚠️ リクエストを読み取れませんでした。"

    token = str(payload.get("token") or "")
    if payload.get("type") != INTERACTION_APPLICATION_COMMAND:
        return token, "⚠️ 未対応のインタラクションです。"

    try:
        message = handle_application_command(
            payload, config=config, store=store, discord=discord, sync_run=sync_run
        )
    except Exception:
        log.exception("deferred interaction の処理に失敗")
        message = "⚠️ 設定処理に失敗しました。時間をおいてもう一度お試しください。"
    return token, message
