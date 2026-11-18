"""作成済みの Discord スケジュールイベントを一覧表示する（確認用）。"""

from __future__ import annotations

import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import requests

API = "https://discord.com/api/v10"


def main() -> int:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if not token or not guild_id:
        print("DISCORD_BOT_TOKEN / DISCORD_GUILD_ID が未設定です")
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": f"Bot {token}"})
    response = session.get(f"{API}/guilds/{guild_id}/scheduled-events", timeout=20)
    if response.status_code != 200:
        print(f"取得失敗: HTTP {response.status_code} {response.text[:200]}")
        return 2

    events = response.json()
    if not events:
        print("イベントはありません")
        return 0

    print(f"{len(events)} 件のイベント:")
    for event in events:
        start = event.get("scheduled_start_time")
        end = event.get("scheduled_end_time")
        status = event.get("status")
        location = (event.get("entity_metadata") or {}).get("location")
        print(f"- {event.get('name')}")
        print(f"  期間: {start} ～ {end} / status={status}")
        print(f"  場所: {location}")
        print(f"  URL: https://discord.com/events/{guild_id}/{event.get('id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
