"""Azure Functions の入口（Python v2 プログラミングモデル）。

ロジックは cloud/ と pso2boost/ に置き、ここは薄く保つ。

- weekly_sync: 毎週水曜 04:00 UTC（= 13:00 JST）の週次バッチ
- health: デプロイ確認用
- setup 系: OAuth 設定フロー（旧 /setup コマンドの代替。
  ブラウザ経由なので Discord の3秒制約を受けない）
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import azure.functions as func

from cloud import config as cloud_config
from cloud import runner, setup_pages
from cloud.storage import TableStore

app = func.FunctionApp()


def _cookies(req: func.HttpRequest) -> dict:
    raw = req.headers.get("Cookie") or ""
    result: dict[str, str] = {}
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            result[name] = value
    return result


def _response(page: setup_pages.Page) -> func.HttpResponse:
    return func.HttpResponse(
        page.body,
        status_code=page.status,
        headers=page.headers,
        mimetype="text/html" if page.body else None,
        charset="utf-8" if page.body else None,
    )


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
        "client_secret": bool(config.client_secret),
    }
    return func.HttpResponse(
        json.dumps(body, ensure_ascii=False), status_code=200, mimetype="application/json"
    )


@app.function_name(name="setup_start")
@app.route(route="setup", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def setup_start(req: func.HttpRequest) -> func.HttpResponse:
    """設定フローの入口。Discord の認可画面へリダイレクトする。"""
    return _response(setup_pages.start(cloud_config.CloudConfig.from_env()))


@app.function_name(name="setup_callback")
@app.route(route="setup/callback", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def setup_callback(req: func.HttpRequest) -> func.HttpResponse:
    """認可コードを受け取り、設定できるサーバー一覧を表示する。"""
    config = cloud_config.CloudConfig.from_env()
    store = TableStore(config.storage_connection)
    return _response(
        setup_pages.callback(
            config, store, query=dict(req.params), cookies=_cookies(req)
        )
    )


@app.function_name(name="setup_channels")
@app.route(route="setup/channels", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def setup_channels(req: func.HttpRequest) -> func.HttpResponse:
    """選んだサーバーのテキストチャンネル一覧を表示する。"""
    config = cloud_config.CloudConfig.from_env()
    store = TableStore(config.storage_connection)
    return _response(
        setup_pages.channels(
            config, store, query=dict(req.params), cookies=_cookies(req)
        )
    )


@app.function_name(name="setup_save")
@app.route(route="setup/save", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def setup_save(req: func.HttpRequest) -> func.HttpResponse:
    """通知先チャンネルを検証して Table へ保存する。"""
    config = cloud_config.CloudConfig.from_env()
    form = {
        key: values[-1]
        for key, values in parse_qs(req.get_body().decode("utf-8")).items()
    }
    store = TableStore(config.storage_connection)
    return _response(
        setup_pages.save(config, store, form=form, cookies=_cookies(req))
    )
