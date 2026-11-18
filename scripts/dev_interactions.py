"""ローカルの Function App に対して署名済みインタラクションを送る開発ツール。

Discord は Ed25519 の秘密鍵を持っているため本番の署名は作れないが、
ローカル開発では「自分用のテスト鍵ペア」を作り、local.settings.json の
DISCORD_PUBLIC_KEY をその公開鍵に差し替えれば、/api/interactions の
全経路（署名検証 → PING → /setup → Table 保存 → ephemeral 応答）を
本番の Discord に影響させずに再現できる。

使い方::

    python scripts/dev_interactions.py init                  # テスト鍵を作る
    python scripts/dev_interactions.py ping                  # PING を送る
    python scripts/dev_interactions.py setup --channel 555   # /setup を送る
    python scripts/dev_interactions.py setup --channel 555 --guild 999

鍵ファイルは Temp の下に置く（リポジトリには残さない）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ENDPOINT = os.environ.get("FUNC_ENDPOINT", "http://127.0.0.1:7071/api/interactions")
KEY_FILE = Path(tempfile.gettempdir()) / "opencode" / "dev-signing.json"


def load_or_create_keys() -> tuple[str, str]:
    from nacl.signing import SigningKey

    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        data = json.loads(KEY_FILE.read_text(encoding="utf-8"))
        return data["secret"], data["public"]

    key = SigningKey.generate()
    payload = {
        "secret": key.encode().hex(),
        "public": key.verify_key.encode().hex(),
    }
    KEY_FILE.write_text(json.dumps(payload), encoding="utf-8")
    return payload["secret"], payload["public"]


def signed_post(body: bytes) -> tuple[int, str, float]:
    from nacl.signing import SigningKey

    secret, _public = load_or_create_keys()
    key = SigningKey(bytes.fromhex(secret))
    timestamp = str(int(time.time()))
    signature = key.sign(timestamp.encode() + body).signature

    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature-Ed25519": signature.hex(),
            "X-Signature-Timestamp": timestamp,
        },
        method="POST",
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status, text = response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status, text = exc.code, exc.read().decode("utf-8")
    elapsed_ms = (time.perf_counter() - start) * 1000
    return status, text, elapsed_ms


def build_payload(args) -> dict:
    member_permissions = str(1 << 5)  # MANAGE_GUILD
    if args.no_permission:
        member_permissions = "0"
    app_permissions = "17600775982080"  # 推奨権限（CREATE/MANAGE_EVENTS + SEND + VIEW）
    if args.no_bot_permission:
        app_permissions = "0"

    return {
        "type": 2,
        "guild_id": args.guild,
        "data": {
            "name": "setup",
            "options": [{"name": "channel", "value": str(args.channel)}],
        },
        "member": {"permissions": member_permissions},
        "app_permissions": app_permissions,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="テスト用の鍵ペアを作る")
    ping = sub.add_parser("ping", help="署名済み PING を送る")

    setup = sub.add_parser("setup", help="署名済み /setup を送る")
    setup.add_argument("--channel", required=True)
    setup.add_argument("--guild", default="111000111")
    setup.add_argument("--no-permission", action="store_true", help="MANAGE_GUILD 無しを疑似的に送る")
    setup.add_argument("--no-bot-permission", action="store_true", help="Bot 権限不足を疑似的に送る")

    args = parser.parse_args(argv)

    if args.command == "init":
        _secret, public = load_or_create_keys()
        print(f"DISCORD_PUBLIC_KEY={public}")
        print("↑この値を local.settings.json の DISCORD_PUBLIC_KEY に貼ってください")
        return 0

    if args.command == "ping":
        status, text, elapsed = signed_post(json.dumps({"type": 1}).encode())
    else:
        payload = build_payload(args)
        status, text, elapsed = signed_post(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

    print(f"HTTP {status} ({elapsed:.0f}ms)")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
