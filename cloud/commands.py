"""スラッシュコマンドの登録（``/setup``）。

グローバル登録は伝播に最大1時間かかるため、開発中は
``DISCORD_DEV_GUILD_ID`` を指定するとギルドコマンドとして即時反映される。

使い方::

    DISCORD_BOT_TOKEN=... DISCORD_APPLICATION_ID=... python -m cloud.commands
    DISCORD_DEV_GUILD_ID=... を足すとギルドコマンドになる
"""

from __future__ import annotations

import argparse
import json
import logging
import os

import requests

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"

MANAGE_GUILD = 1 << 5

# CHANNEL 型のオプション。テキスト（0）とアナウンス（5）だけ選べる
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
        return (
            f"{API_BASE}/applications/{application_id}"
            f"/guilds/{dev_guild_id}/commands"
        )
    return f"{API_BASE}/applications/{application_id}/commands"


def register_commands(
    token: str,
    application_id: str,
    *,
    dev_guild_id: str | None = None,
    session=None,
    timeout: float = 20.0,
) -> list[dict]:
    """``/setup`` を登録（既存のコマンドをまとめて上書きする PUT）。"""
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
    parser.add_argument("--dev-guild", help="開発中に即時反映させるギルド ID")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    application_id = os.getenv("DISCORD_APPLICATION_ID", "").strip()
    dev_guild = (args.dev_guild or os.getenv("DISCORD_DEV_GUILD_ID", "")).strip()
    if not token or not application_id:
        log.error("DISCORD_BOT_TOKEN / DISCORD_APPLICATION_ID を設定してください")
        return 2

    result = register_commands(token, application_id, dev_guild_id=dev_guild or None)
    where = f"ギルド {dev_guild}" if dev_guild else "グローバル"
    log.info("%s へ /setup を登録しました", where)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
