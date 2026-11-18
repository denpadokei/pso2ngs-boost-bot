"""Discord OAuth2 によるサーバー設定フロー（旧 /setup の代替）。

HTTP Interactions には「3秒以内の応答」というハードな制約があり、
スケールゼロのサーバーレスと相性が悪い（インスタンス再確保の待ち時間で
応答が間に合わないことがある）。設定フローをブラウザ経由の OAuth2 に
移すことで、その制約そのものを消す。

- 認可: scope=identify guilds（ユーザー本人と、管理権限のあるサーバー一覧）
- コールバックでコードをアクセストークンに交換して **1回だけ使う**。
  保存しないので、リフレッシュトークンの管理は不要。
- CSRF 対策: state（ランダム）をクッキーとクエリで照合。
- サーバー側セッション: 15分有効の HMAC 署名付きクッキー
  （ユーザーIDと、認可時に管理権限があったサーバーIDのリスト）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import requests

API_BASE = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"

MANAGE_GUILD = 1 << 5
SETUP_SCOPES = "identify guilds"

SESSION_TTL_SECONDS = 15 * 60


class OAuthError(RuntimeError):
    """OAuth の交換や API 確認に失敗した。"""


# ------------------------------------------------------------------ 認可 URL
def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """ユーザーを Discord の認可画面へ送る URL。"""
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SETUP_SCOPES,
            "state": state,
            "prompt": "none",  # 認可済みならそのまま戻ってくる
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def make_state() -> str:
    return secrets.token_hex(16)


# ------------------------------------------------------------ コード交換・API
def exchange_code(
    session: requests.Session,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict:
    """認可コードをアクセストークンへ交換する（1回きり）。"""
    response = session.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=20,
    )
    if response.status_code >= 400:
        raise OAuthError(
            f"トークン交換に失敗: HTTP {response.status_code}: {response.text[:200]}"
        )
    return response.json()


def get_user(session: requests.Session, access_token: str) -> dict:
    response = session.get(
        f"{API_BASE}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if response.status_code >= 400:
        raise OAuthError(f"ユーザー取得に失敗: HTTP {response.status_code}")
    return response.json()


def get_user_guilds(session: requests.Session, access_token: str) -> list[dict]:
    response = session.get(
        f"{API_BASE}/users/@me/guilds",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if response.status_code >= 400:
        raise OAuthError(f"サーバー一覧の取得に失敗: HTTP {response.status_code}")
    guilds = response.json()
    return guilds if isinstance(guilds, list) else []


def manageable_guilds(guilds: list[dict]) -> list[dict]:
    """「サーバーを管理」権限を持っているサーバーだけを残す。"""
    result = []
    for guild in guilds:
        try:
            permissions = int(str(guild.get("permissions") or "0"))
        except ValueError:
            continue
        if permissions & MANAGE_GUILD:
            result.append(guild)
    return result


# ---------------------------------------------------------------- 署名セッション
def _sign(secret: bytes, payload: bytes) -> bytes:
    return hmac.new(secret, payload, hashlib.sha256).digest()


def issue_session(
    secret: str, *, user_id: str, username: str, guild_ids: list[str], now: float | None = None
) -> str:
    """15分有効の署名付きセッショントークンを作る。

    値は base64(json) + "." + base64(hmac)。サーバー側に状態を持たない。
    """
    moment = now if now is not None else time.time()
    payload = json.dumps(
        {
            "uid": str(user_id),
            "name": str(username),
            "guilds": [str(gid) for gid in guild_ids],
            "exp": int(moment) + SESSION_TTL_SECONDS,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    body = base64.urlsafe_b64encode(payload)
    signature = base64.urlsafe_b64encode(_sign(secret.encode("utf-8"), body))
    return f"{body.decode('ascii')}.{signature.decode('ascii')}"


def load_session(secret: str, token: str) -> dict | None:
    """署名と有効期限を検証して、セッション内容を返す。不正なら None。"""
    if not token or "." not in token:
        return None
    body_part, signature_part = token.split(".", 1)
    try:
        body = base64.urlsafe_b64decode(body_part.encode("ascii") + b"==")
        signature = base64.urlsafe_b64decode(
            signature_part.encode("ascii") + b"=="
        )
    except (ValueError, UnicodeError):
        return None
    expected = _sign(secret.encode("utf-8"), base64.urlsafe_b64encode(body))
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp") or 0) < time.time():
        return None
    return payload
