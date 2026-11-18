"""Azure Functions の入口（Python v2 プログラミングモデル）。

ロジックは cloud/ と pso2boost/ に置き、ここは薄く保つ。

注意: defer-try ブランチでは keep-alive を意図的に外している。
コールドスタート条件下での deferReply の挙動を実測するため。
"""

from __future__ import annotations

import json

import azure.functions as func

from cloud import config as cloud_config
from cloud import interactions, runner

app = func.FunctionApp()


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
