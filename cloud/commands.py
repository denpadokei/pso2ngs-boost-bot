"""スラッシュコマンド ``/setup`` を Discord へ登録する。

グローバル登録は伝播に最大1時間かかるため、開発中は
``DISCORD_DEV_GUILD_ID`` を指定するとギルドコマンドとして即時反映される。

使い方::

    DISCORD_BOT_TOKEN=... DISCORD_APPLICATION_ID=... python -m cloud.commands
    DISCORD_DEV_GUILD_ID=... を足すとギルドコマンドになる
"""

from __future__ import annotations

import argparse
import logging
import os

import requests

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
MANAGE_GUILD = 1 << 5

SETUP_COMMAND = {
    "name": "setup",
    "description": "ブーストキャンペーンの通知先チャンネルを設定します",
    "default_member_permissions": str(MANAGE_GUILD),
    "dm_permission": False,
    "options": [
        {
            "name": "channel",
            "description": "通知を投稿するチャンネル",
            "type": 7,  # CHANNEL
            "required": True,
            "channel_types": [0, 5],
        }
    ],
}


def commands_url(application_id: str, dev_guild_id: str | None = None) -> str:
    if dev_guild_id:
        return f"{API_BASE}/applications/{application_id}/guilds/{dev_guild_id}/commands"
    return f"{API_BASE}/applications/{application_id}/commands"


def register_commands(
    token: str,
    application_id: str,
    *,
    dev_guild_id: str | None = None,
    session=None,
    timeout: float = 20.0,
) -> list[dict]:
    """``/setup`` を登録する（PUT で既存コマンドをまとめて上書き）。"""
    session = session or requests.Session()
    session.headers.update(
        {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    )
    response = session.put(
        commands_url(application_id, dev_guild_id),
        json=[SETUP_COMMAND],
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"コマンドの登録に失敗: HTTP {response.status_code} {response.text[:300]}"
        )
    return response.json()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cloud.commands", description="Discord に /setup を登録する"
    )
    parser.add_argument("--dev-guild", help="開発中に即時反映させるギルドID")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    application_id = os.environ.get("DISCORD_APPLICATION_ID", "").strip()
    if not token or not application_id:
        parser.error("DISCORD_BOT_TOKEN と DISCORD_APPLICATION_ID が必要です")

    commands = register_commands(
        token,
        application_id,
        dev_guild_id=args.dev_guild or os.environ.get("DISCORD_DEV_GUILD_ID") or None,
    )
    for command in commands:
        log.info("registered: /%s (%s)", command.get("name"), command.get("id"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
