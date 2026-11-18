"""OAuth 設定フローのページ（旧 /setup の代替）。

ブラウザ向けの最小限の HTML を返す。azure.functions に依存せず、
各ハンドラは (HTTPステータス, 本文, ヘッダー) を返すだけにして
単体テストできるようにする。

流れ:
1. start        → Discord の認可画面へリダイレクト（state をクッキーに保存）
2. callback     → コード交換 → 「サーバーを管理」できるサーバー一覧を表示。
                  サーバー側セッション（Table）を作り、クッキーには ID だけ載せる
3. channels     → 選んだサーバーのテキストチャンネル一覧を表示
4. save         → 検証して Table へ保存 → 完了ページ
"""

from __future__ import annotations

import html
import time
from urllib.parse import urlencode

import requests

from pso2boost.discord_events import RECOMMENDED_PERMISSIONS
from pso2boost.lookup import TEXT_CHANNEL_TYPES

from . import oauth
from .config import CloudConfig
from .discord import DiscordRest, DiscordRestError
from .storage import GuildConfig, TableStore
from pso2boost.tz import now_jst

STATE_COOKIE = "pso2state"
SESSION_COOKIE = "pso2session"


class Page:
    """ハンドラの戻り値。function_app が HttpResponse へ変換する。"""

    def __init__(self, status: int, body: str = "", headers: dict | None = None) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}


# ------------------------------------------------------------------ HTML 部品
STYLE = """
body { font-family: system-ui, sans-serif; background: #1e1f22; color: #f2f3f5;
       max-width: 560px; margin: 48px auto; padding: 0 16px; }
.card { background: #2b2d31; border-radius: 12px; padding: 24px; }
h1 { font-size: 1.2rem; margin: 0 0 12px; }
ul { list-style: none; padding: 0; margin: 16px 0; }
li { margin: 8px 0; }
a, .button { color: #00a8fc; text-decoration: none; }
.button { display: inline-block; background: #5865f2; color: #fff;
          padding: 10px 18px; border-radius: 8px; border: none;
          font-size: 1rem; cursor: pointer; }
select { font-size: 1rem; padding: 8px; border-radius: 6px;
         background: #1e1f22; color: #f2f3f5; border: 1px solid #3f4148; }
.note { color: #b5bac1; font-size: 0.9rem; }
.error { color: #f77; }
"""


def render_page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{STYLE}</style>
</head>
<body>
<div class="card">
{body}
</div>
</body>
</html>"""


def _error_page(message: str) -> str:
    return render_page(
        "エラー",
        f"<h1>⚠️ {html.escape(message)}</h1>"
        '<p class="note">しばらくしてからもう一度お試しください。</p>',
    )


def _cookie(name: str, value: str, max_age: int) -> str:
    return (
        f"{name}={value}; Path=/api/setup; HttpOnly; SameSite=Lax; Max-Age={max_age}"
    )


def bot_invite_url(client_id: str, guild_id: str | None = None) -> str:
    query = {"client_id": client_id, "scope": "bot", "permissions": RECOMMENDED_PERMISSIONS}
    if guild_id:
        query["guild_id"] = guild_id
    return f"https://discord.com/oauth2/authorize?{urlencode(query)}"


# ------------------------------------------------------------------ 1. 開始
def start(config: CloudConfig) -> Page:
    state = oauth.make_state()
    location = oauth.authorize_url(
        config.client_id, f"https://{config.host_name}/api/setup/callback", state
    )
    return Page(
        302,
        "",
        {
            "Location": location,
            "Set-Cookie": _cookie(STATE_COOKIE, state, 10 * 60),
        },
    )


# -------------------------------------------------------------- 2. コールバック
def callback(
    config: CloudConfig,
    store: TableStore,
    *,
    query: dict,
    cookies: dict,
    session: requests.Session | None = None,
) -> Page:
    session = session or requests.Session()

    if query.get("error"):
        return Page(200, _error_page("認可がキャンセルされました。"))

    state = str(query.get("state") or "")
    if not state or state != cookies.get(STATE_COOKIE):
        return Page(400, _error_page("状態の確認に失敗しました。"))

    code = str(query.get("code") or "")
    if not code:
        return Page(400, _error_page("認可コードがありません。"))

    try:
        token = oauth.exchange_code(
            session,
            client_id=config.client_id,
            client_secret=config.client_secret,
            code=code,
            redirect_uri=f"https://{config.host_name}/api/setup/callback",
        )
        user = oauth.get_user(session, token.get("access_token", ""))
        guilds = oauth.get_user_guilds(session, token.get("access_token", ""))
    except oauth.OAuthError as exc:
        return Page(200, _error_page(f"Discord との連携に失敗しました: {exc}"))

    manageable = oauth.manageable_guilds(guilds)
    if not manageable:
        return Page(
            200,
            render_page(
                "サーバー設定",
                "<h1>設定できるサーバーがありません</h1>"
                "<p class=note>「サーバーを管理」権限のあるサーバーで、"
                "このページからもう一度設定してください。</p>",
            ),
        )

    items = "\n".join(
        f'<li><a class="button" href="/api/setup/channels?guild_id={html.escape(str(g.get("id")))}">'
        f"{html.escape(str(g.get('name') or g.get('id')))} へ</a></li>"
        for g in sorted(manageable, key=lambda g: str(g.get("name", "")))
    )
    body = (
        f"<h1>どのサーバーに設定しますか？</h1>"
        f'<p class="note">{html.escape(str(user.get("username") or ""))} さんが管理できるサーバー:</p>'
        f"<ul>{items}</ul>"
        '<p class="note">通知先はこのあと選べます。毎週水曜 13:00 (JST) に通知します。</p>'
    )
    token_value = oauth.make_session_id()
    try:
        store.ensure_tables()
        store.put_session(
            token_value,
            {
                "uid": str(user.get("id") or ""),
                "name": str(user.get("global_name") or user.get("username") or ""),
                "guilds": [str(g.get("id")) for g in manageable],
                "exp": oauth.session_expiry(),
            },
            now_jst(),
        )
    except Exception as exc:
        return Page(200, _error_page(f"セッションの保存に失敗しました: {exc}"))
    return Page(
        200,
        render_page("サーバー設定", body),
        {"Set-Cookie": _cookie(SESSION_COOKIE, token_value, oauth.SESSION_TTL_SECONDS)},
    )


# ------------------------------------------------------------------ 3. チャンネル
def channels(
    config: CloudConfig,
    store: TableStore,
    *,
    query: dict,
    cookies: dict,
    discord: DiscordRest | None = None,
) -> Page:
    payload = store.get_session(cookies.get(SESSION_COOKIE, ""), now_jst())
    if payload is None:
        return Page(403, _error_page("セッションの有効期限が切れています。"))

    guild_id = str(query.get("guild_id") or "")
    if not guild_id or guild_id not in payload.get("guilds", []):
        return Page(403, _error_page("このサーバーの設定権限がありません。"))

    discord = discord or DiscordRest(config.bot_token)
    try:
        channels_list = discord.get_guild_channels(guild_id)
    except DiscordRestError as exc:
        if exc.status_code in (403, 404):
            invite = bot_invite_url(config.client_id, guild_id)
            return Page(
                200,
                render_page(
                    "Bot の招待",
                    "<h1>Bot がこのサーバーにいません</h1>"
                    '<p class="note">まず Bot をサーバーへ追加してください。</p>'
                    f'<p><a class="button" href="{html.escape(invite)}">Bot を招待する</a></p>'
                    '<p class="note">招待したら <a href="/api/setup">最初からやり直す</a>と'
                    "チャンネルを選べます。</p>",
                ),
            )
        return Page(200, _error_page(f"チャンネル一覧を取得できませんでした: {exc}"))

    options = "\n".join(
        f'<option value="{html.escape(str(c.get("id")))}">#{html.escape(str(c.get("name") or c.get("id")))}</option>'
        for c in sorted(channels_list, key=lambda c: str(c.get("name", "")))
        if c.get("type") in TEXT_CHANNEL_TYPES
    )
    if not options:
        return Page(200, _error_page("テキストチャンネルが見つかりません。"))

    body = (
        "<h1>通知先チャンネルを選んでください</h1>"
        f'<form method="post" action="/api/setup/save">'
        f'<input type="hidden" name="guild_id" value="{html.escape(guild_id)}">'
        f'<p><select name="channel_id">{options}</select></p>'
        f'<p><button class="button" type="submit">設定する</button></p>'
        "</form>"
        '<p class="note">毎週水曜 13:00 (JST) にブーストキャンペーンの通知と'
        "イベント作成を行います。</p>"
    )
    return Page(200, render_page("通知先の選択", body))


# ------------------------------------------------------------------ 5. 手動同期
def sync_ready(
    config: CloudConfig,
    store: TableStore,
    *,
    cookies: dict,
) -> Page:
    """手動同期の確認ページ（実行ボタンだけを出す）。"""
    payload = store.get_session(cookies.get(SESSION_COOKIE, ""), now_jst())
    if payload is None:
        return Page(403, _error_page("セッションの有効期限が切れています。設定ページを開き直してください。"))

    body = (
        "<h1>今すぐ同期しますか？</h1>"
        "<p>週次バッチと同じ処理をすぐに実行します。</p>"
        "<ul>"
        "<li>新しいキャンペーンの通知</li>"
        "<li>Discord イベントの作成・更新・終了後の削除</li>"
        "</ul>"
        '<p class="note">10秒〜1分ほどかかります。実行中はページを閉じないでください。</p>'
        '<form method="post" action="/api/setup/sync">'
        '<button class="button" type="submit">同期を実行</button>'
        "</form>"
        '<p class="note"><a href="/api/setup">設定ページへ戻る</a></p>'
    )
    return Page(200, render_page("今すぐ同期", body))


def sync_now(
    config: CloudConfig,
    store: TableStore,
    *,
    cookies: dict,
    run=None,
) -> Page:
    """週次バッチを即時実行し、結果を表示する。

    ``run`` はテスト用の差し替え（省略時は cloud.runner.run_weekly）。
    """
    payload = store.get_session(cookies.get(SESSION_COOKIE, ""), now_jst())
    if payload is None:
        return Page(403, _error_page("セッションの有効期限が切れています。設定ページを開き直してください。"))

    if run is None:
        from . import runner as runner_module

        run = lambda: runner_module.run_weekly(config, store=store)

    try:
        summary = run()
    except Exception as exc:
        return Page(200, _error_page(f"同期に失敗しました: {exc}"))

    rows = "".join(
        f"<li>サーバー {html.escape(d.guild_id)}: 通知 {d.notified} / "
        f"作成 {d.created} / 更新 {d.updated} / 削除 {d.deleted}"
        + (f"<br><span class='error'>{html.escape(d.last_error)}</span>" if d.last_error else "")
        + "</li>"
        for d in getattr(summary, "details", [])
    )
    body = (
        "<h1>✅ 同期が完了しました</h1>"
        f"<p>通知 {summary.notified} / イベント作成 {summary.created} / "
        f"更新 {summary.updated} / 削除 {summary.deleted}"
        + (f" / 失敗 {summary.failed}" if getattr(summary, "failed", 0) else "")
        + "</p>"
        + (f"<ul>{rows}</ul>" if rows else "")
        + '<p class="note"><a href="/api/setup">設定ページへ戻る</a></p>'
    )
    return Page(200, render_page("同期結果", body))

# ------------------------------------------------------------------ 4. 保存
def save(
    config: CloudConfig,
    store: TableStore,
    *,
    form: dict,
    cookies: dict,
    discord: DiscordRest | None = None,
) -> Page:
    payload = store.get_session(cookies.get(SESSION_COOKIE, ""), now_jst())
    if payload is None:
        return Page(403, _error_page("セッションの有効期限が切れています。"))

    guild_id = str(form.get("guild_id") or "")
    channel_id = str(form.get("channel_id") or "")
    if not guild_id or guild_id not in payload.get("guilds", []):
        return Page(403, _error_page("このサーバーの設定権限がありません。"))
    if not channel_id:
        return Page(400, _error_page("チャンネルが選ばれていません。"))

    discord = discord or DiscordRest(config.bot_token)
    try:
        channel = discord.get_channel(channel_id)
    except DiscordRestError as exc:
        return Page(200, _error_page(f"チャンネルを確認できませんでした: {exc}"))
    if str(channel.get("guild_id") or "") != guild_id:
        return Page(400, _error_page("このサーバー内のチャンネルを指定してください。"))
    if channel.get("type") not in TEXT_CHANNEL_TYPES:
        return Page(400, _error_page("テキストチャンネルを指定してください。"))

    now = now_jst()
    try:
        store.ensure_tables()
        existing = {
            g.guild_id: g for g in store.list_guilds()
        }.get(guild_id)
        store.upsert_guild(
            GuildConfig(
                guild_id=guild_id,
                channel_id=str(channel.get("id") or channel_id),
                mention_role_id=existing.mention_role_id if existing else "",
                enabled=True,
                permissions_ok=True,
                added_at=existing.added_at if existing else now.isoformat(),
                last_run_at=existing.last_run_at if existing else "",
                last_error="",
            )
        )
    except Exception as exc:  # どんな失敗でもページで案内する
        return Page(200, _error_page(f"設定の保存に失敗しました: {exc}"))

    body = (
        f"<h1>✅ 通知先を #{html.escape(str(channel.get('name') or channel_id))} に設定しました</h1>"
        "<p>毎週水曜 13:00 (JST) にブーストキャンペーンの通知とイベント作成を行います。</p>"
        '<form method="post" action="/api/setup/sync">'
        '<button class="button" type="submit">今すぐイベントを同期する</button>'
        "</form>"
        '<p class="note">待たずに今すぐ反映したい場合はこちら（10秒〜1分ほどかかります）。</p>'
        '<p class="note">Bot をサーバーから抜くと、次の週次実行で自動的に停止します。</p>'
    )
    return Page(200, render_page("設定完了", body))
