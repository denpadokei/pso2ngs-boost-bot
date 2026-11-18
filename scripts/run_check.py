"""秘密カード経由で実行するワンショットスクリプト。

1. Bot トークン・サーバー・チャンネルを読み取り専用で検証
2. イベント作成に必要な権限（CREATE_EVENTS / MANAGE_EVENTS）を確認
3. 問題なければ通常の check を実行（イベント作成 + 通知投稿）

秘密は環境変数から読み、画面には出さない。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from pso2boost.cli import main  # noqa: E402
from pso2boost.discord_events import REQUIRED_PERMISSIONS  # noqa: E402

API = "https://discord.com/api/v10"
SEND_MESSAGES = 1 << 11
VIEW_CHANNEL = 1 << 10

PERMISSION_NAMES = {
    1 << 33: "イベントの管理 (MANAGE_EVENTS)",
    1 << 44: "イベントの作成 (CREATE_EVENTS)",
    SEND_MESSAGES: "メッセージを送信 (SEND_MESSAGES)",
    VIEW_CHANNEL: "チャンネルを見る (VIEW_CHANNEL)",
}


def bit_names(value: int) -> str:
    names = [name for bit, name in PERMISSION_NAMES.items() if value & bit]
    return ", ".join(names) or "なし"


def bot_permissions(session: requests.Session, guild_id: str, bot_id: str) -> int | None:
    guild = session.get(f"{API}/guilds/{guild_id}", timeout=20)
    if guild.status_code != 200:
        return None
    if guild.json().get("owner_id") == bot_id:
        return ~0

    member = session.get(f"{API}/guilds/{guild_id}/members/{bot_id}", timeout=20)
    if member.status_code != 200:
        return None

    roles = session.get(f"{API}/guilds/{guild_id}/roles", timeout=20)
    if roles.status_code != 200:
        return None

    role_ids = set(member.json().get("roles", [])) | {guild_id}
    permissions = 0
    for role in roles.json():
        if str(role["id"]) in role_ids:
            permissions |= int(role["permissions"])
    return permissions


def main_check() -> int:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    channel_id = os.environ.get("DISCORD_CHANNEL_ID", "").strip()

    if not token or not guild_id:
        print("NG: DISCORD_BOT_TOKEN / DISCORD_GUILD_ID が未設定です")
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": f"Bot {token}"})

    me = session.get(f"{API}/users/@me", timeout=20)
    if me.status_code != 200:
        print(f"NG: Bot トークンが無効です (HTTP {me.status_code})")
        return 2
    bot = me.json()
    print(f"OK: Bot = {bot.get('username')} ({bot.get('id')})")

    guild = session.get(f"{API}/guilds/{guild_id}", timeout=20)
    if guild.status_code != 200:
        print(f"NG: サーバーにアクセスできません (HTTP {guild.status_code})")
        print(f"    指定した ID: {guild_id}")
        joined = session.get(f"{API}/users/@me/guilds", timeout=20)
        if joined.status_code == 200:
            guilds = joined.json()
            if guilds:
                print("この Bot が参加しているサーバー:")
                for item in guilds:
                    print(f"  - {item.get('name')}  id={item.get('id')}")
            else:
                print("この Bot はどのサーバーにも参加していません。招待してください。")
        print("    → 上の一覧にある ID を DISCORD_GUILD_ID に指定してください")
        return 2
    print(f"OK: サーバー = {guild.json().get('name')}")

    permissions = bot_permissions(session, guild_id, str(bot.get("id")))
    if permissions is None:
        print("NG: サーバーの権限を取得できませんでした（Bot が未参加の可能性）")
        return 2
    print(f"OK: サーバー権限 = {bit_names(permissions)}")

    missing = REQUIRED_PERMISSIONS & ~permissions
    if missing:
        print(f"NG: イベント作成に必要な権限が足りません: {bit_names(missing)}")
        print("    サーバー設定 → メンバー → Bot のロールで権限を付与してください")
        return 2

    if channel_id:
        channel = session.get(f"{API}/channels/{channel_id}", timeout=20)
        if channel.status_code != 200:
            print(f"NG: チャンネルを取得できません (HTTP {channel.status_code})")
            return 2
        data = channel.json()
        print(f"OK: チャンネル = #{data.get('name')} (type={data.get('type')})")
        channel_perms = permissions & (SEND_MESSAGES | VIEW_CHANNEL)
        if channel_perms != (SEND_MESSAGES | VIEW_CHANNEL):
            print(f"警告: チャンネル権限が不足している可能性: {bit_names(channel_perms)}")
    else:
        print("注意: DISCORD_CHANNEL_ID 未設定のため投稿は行いません")

    print("--- check を実行します（イベント作成 + 通知投稿）---")
    return main(["check", "--state", str(ROOT / "data" / "state.json")])


if __name__ == "__main__":
    sys.exit(main_check())
