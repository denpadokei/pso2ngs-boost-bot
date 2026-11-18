"""週次バッチのスモークテスト（実サイト取得 + Fake の Discord/Table 先）。

ローカル実行専用。tests/ から import して使う。
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # リポジトリルート

from cloud.config import CloudConfig
from cloud.runner import run_weekly
from cloud.storage import GuildConfig, TableStore
from fakes import FakeApi, FakeDiscordRest, FakeNotifier, FakeTableService
from pso2boost.tz import now_jst

service = FakeTableService()
store = TableStore("unused", service=service)
store.ensure_tables()
store.upsert_guild(GuildConfig(guild_id="111", channel_id="chan-A"))
api = FakeApi("111")
notifier = FakeNotifier("chan-A")

config = CloudConfig(bot_token="smoke", storage_connection="unused", delay=0.2)
summary = run_weekly(
    config,
    now=now_jst(),
    store=store,
    guild_client=FakeDiscordRest(),
    api_factory=lambda target: api,
    notifier_factory=lambda target: notifier,
)

print("summary:", summary.summary)
for kind, key, url in notifier.sent:
    print(f"  notify: [{kind}] {key} -> {url}")
print(f"  discord events created: {len(api.created)}")
assert summary.guilds == 1
assert notifier.sent, "通知が1件もありません"
assert api.created, "Discord イベントが作成されていません"
assert summary.failed == 0
print("SMOKE OK")
