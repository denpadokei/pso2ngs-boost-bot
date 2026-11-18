"""Azure Functions の入口（Python v2 プログラミングモデル）。

ロジックは cloud/ と pso2boost/ に置き、ここは薄く保つ。
"""

from __future__ import annotations

import json

import azure.functions as func
import requests

from cloud import config as cloud_config
from cloud import interactions, runner

app = func.FunctionApp()


@app.timer_trigger(
    arg_name="timer",
    schedule="0 */4 * * * *",  # 4分ごと
    run_on_startup=False,
    use_monitor=True,
)
def keep_alive(timer: func.TimerRequest) -> None:
    """HTTP 関数のインスタンスを温存して /setup を3秒以内に応答させる。

    Flex 従量課金はアイドルでスケールゼロになり、コールドスタートが
    Discord の応答期限（3秒）を超えることがある。自分自身の /api/health
    を呼ぶことで http 関数グループのインスタンスを確保しておく。
    （/setup の保存自体はコールドスタートでも成功するが、ephemeral
    応答が Discord 側で「応答なし」表示になるのを防ぐため）
    """
    import os

    hostname = os.environ.get("WEBSITE_HOSTNAME", "")
    if not hostname:
        return
    try:
        requests.get(f"https://{hostname}/api/health", timeout=10)
    except requests.RequestException:
        pass  # 失敗しても次のタイマーで再試行するだけなので無視


@app.timer_trigger(
    arg_name="timer",
    schedule="0 0 4 * * 3",  # 毎週水曜 04:00 UTC = 13:00 JST
    run_on_startup=False,
    use_monitor=True,
)
def weekly_sync(timer: func.TimerRequest) -> None:
    """全サーバーの週次同期（新着・開始・終了の通知とイベント作成）。"""
    runner.run_weekly(cloud_config.CloudConfig.from_env())


@app.function_name(name="health")
@app.route(route="health", auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    """デプロイ確認用。設定の有無だけ返す（秘密は含めない）。"""
    config = cloud_config.CloudConfig.from_env()
    body = {
        "status": "ok",
        "bot_token": bool(config.bot_token),
        "storage": bool(config.storage_connection),
        "public_key": bool(config.public_key),
    }
    return func.HttpResponse(
        json.dumps(body, ensure_ascii=False), status_code=200, mimetype="application/json"
    )


@app.function_name(name="interactions")
@app.route(route="interactions", auth_level=func.AuthLevel.ANONYMOUS)
def discord_interactions(req: func.HttpRequest) -> func.HttpResponse:
    """Discord の HTTP Interactions（PING / /setup）。"""
    config = cloud_config.CloudConfig.from_env()
    status, body = interactions.handle_interaction(
        body=req.get_body(),
        headers=req.headers,
        config=config,
    )
    return func.HttpResponse(
        json.dumps(body, ensure_ascii=False),
        status_code=status,
        mimetype="application/json",
    )
