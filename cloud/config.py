"""共有Bot の設定。環境変数から読むだけにする（秘密はアプリ設定へ）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from pso2boost.schedule import DEFAULT_KEYWORDS


def _keywords(raw: str) -> tuple[str, ...]:
    raw = (raw or "").strip()
    if not raw:
        return DEFAULT_KEYWORDS
    return tuple(word.strip() for word in raw.split(",") if word.strip())


def _float(raw: str, default: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _int(raw: str, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class CloudConfig:
    bot_token: str = ""
    storage_connection: str = ""
    host_name: str = ""  # Function App のホスト名（OAuth の redirect_uri に使う）
    client_id: str = ""  # Discord アプリケーション ID
    client_secret: str = ""  # OAuth クライアントシークレット（設定フロー用）
    guild_id: str = ""  # 環境変数による1サーバー運用（Table 未登録時のフォールバック）
    channel_id: str = ""
    notify_style: str = "discal"
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS
    delay: float = 1.0
    concurrency: int = 4
    state_retention_days: int = 30

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CloudConfig":
        if env is None:
            env = os.environ
        storage = env.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
        if not storage:
            # Function App には必ずある接続文字列へフォールバック
            storage = env.get("AzureWebJobsStorage", "").strip()
        return cls(
            bot_token=env.get("DISCORD_BOT_TOKEN", "").strip(),
            storage_connection=storage,
            host_name=env.get("WEBSITE_HOSTNAME", "").strip(),
            client_id=env.get("DISCORD_APPLICATION_ID", "").strip(),
            client_secret=env.get("DISCORD_CLIENT_SECRET", "").strip(),
            guild_id=env.get("DISCORD_GUILD_ID", "").strip(),
            channel_id=env.get("DISCORD_CHANNEL_ID", "").strip(),
            notify_style=env.get("NOTIFY_STYLE", "discal").strip() or "discal",
            keywords=_keywords(env.get("BOOST_KEYWORDS", "")),
            delay=_float(env.get("PSO2_FETCH_DELAY", ""), 1.0),
            concurrency=_int(env.get("CLOUD_CONCURRENCY", ""), 4),
            state_retention_days=_int(env.get("CLOUD_STATE_RETENTION_DAYS", ""), 30),
        )
