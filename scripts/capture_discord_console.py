"""Discord クライアントのコンソールログを CDP 経由で録る（開発ツール）。

Discord が --remote-debugging-port=9222 付きで起動している前提。
ページターゲットにアタッチし、console / 例外 / ログエントリを
標準出力とログファイルの両方へ書き出す。Ctrl+C またはタイムアウトで終了。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import websocket

CDP = "http://127.0.0.1:9222"
LOG_FILE = Path(__file__).parent / "discord-console.log"
TIMEOUT_SECONDS = 20 * 60


def list_targets() -> list[dict]:
    with urllib.request.urlopen(f"{CDP}/json", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def preview(args: list) -> str:
    parts = []
    for arg in args:
        if isinstance(arg, dict):
            parts.append(arg.get("description") or arg.get("value") or arg.get("type", "?"))
        else:
            parts.append(str(arg))
    return " ".join(str(p) for p in parts)[:400]


def attach(target: dict, sink) -> websocket.WebSocketApp | None:
    url = target.get("webSocketDebuggerUrl")
    if not url or target.get("type") != "page":
        return None

    def on_open(ws):
        ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        ws.send(json.dumps({"id": 2, "method": "Log.enable"}))
        sink(f"=== attached: {target.get('title', '')[:60]}")

    def on_message(_ws, raw):
        event = json.loads(raw)
        method = event.get("method", "")
        params = event.get("params", {})
        stamp = time.strftime("%H:%M:%S")

        if method == "Runtime.consoleAPICalled":
            sink(f"[{stamp}] console.{params.get('type')}: {preview(params.get('args', []))}")
        elif method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails", {})
            sink(f"[{stamp}] EXCEPTION: {json.dumps(detail, ensure_ascii=False)[:400]}")
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            sink(
                f"[{stamp}] log.{entry.get('level')}: "
                f"{entry.get('source')} {entry.get('text', '')[:300]}"
            )

    def on_error(_ws, error):
        sink(f"=== ws error: {error}")

    def on_close(_ws, _code, _reason):
        sink("=== ws closed")

    app = websocket.WebSocketApp(
        url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close
    )
    import threading

    thread = threading.Thread(target=app.run_forever, daemon=True)
    thread.start()
    return app


def main() -> int:
    LOG_FILE.write_text("", encoding="utf-8")  # 毎回クリア

    def sink(message: str) -> None:
        print(message, flush=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(message + "\n")

    print(f"listening for {TIMEOUT_SECONDS // 60} min -> {LOG_FILE}", flush=True)

    attached: dict[str, websocket.WebSocketApp] = {}
    deadline = time.time() + TIMEOUT_SECONDS

    while time.time() < deadline:
        try:
            targets = list_targets()
        except Exception as exc:  # noqa: BLE001
            sink(f"=== cannot reach CDP: {exc}")
            time.sleep(5)
            continue

        for target in targets:
            key = target.get("id", "")
            if key and key not in attached:
                app = attach(target, sink)
                if app:
                    attached[key] = app

        for key, app in list(attached.items()):
            if not app.keep_running:
                attached.pop(key, None)

        time.sleep(3)

    sink("=== timeout, exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
