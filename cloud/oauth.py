"""Discord OAuth2 によるサーバー設定フロー（旧 /setup の代替）。

HTTP Interactions には「3秒以内の応答」というハードな制約があり、
スケールゼロのサーバーレスと相性が悪い（deferReply でも ACK 前の
インスタンス起動待ちは解消できないことを実測で確認済み）。
設定フローをブラウザ経由の OAuth2 に移すことで、その制約そのものを消す。

- 認可: scope=identify guilds（ユーザー本人と、管理権限のあるサーバー一覧）
- コールバックでコードをアクセストークンに交換して **1回だけ使う**。
  保存しないので、リフレッシュトークンの管理は不要。
- CSRF 対策: state（ランダム）をクッキーとクエリで照合。
- サーバー側セッション: ランダム ID をクッキーに持ち、ユーザー情報と
  管理権限のあるサーバーIDは Table に保存する（クッキーを小さく保つ。
  サーバー数が多いとクッキー上限を超えてブラウザに破棄されるため）。
"""

from __future__ import annotations

import logging
import secrets
import time
from urllib.parse import urlencode

import requests

API_BASE = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"

MANAGE_GUILD = 1 << 5
SETUP_SCOPES = "identify guilds"

SESSION_TTL_SECONDS = 15 * 60

log = logging.getLogger(__name__)


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
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def make_state() -> str:
    return secrets.token_hex(16)


def make_session_id() -> str:
    """クッキーに載せる不透明なセッションID（数十バイト）。"""
    return secrets.token_urlsafe(18)


def session_expiry(now: float | None = None) -> int:
    moment = now if now is not None else time.time()
    return int(moment) + SESSION_TTL_SECONDS


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
            log.warning("サーバー %s の権限値を解釈できませんでした", guild.get("id"))
            continue
        if permissions & MANAGE_GUILD:
            result.append(guild)
    return result


