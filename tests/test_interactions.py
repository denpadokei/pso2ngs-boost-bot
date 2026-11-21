"""Cloudflare Worker から転送された deferred interaction のテスト。"""

from __future__ import annotations

import json

import pytest
from nacl.signing import SigningKey

from cloud import interactions
from cloud.config import CloudConfig
from cloud.storage import TableStore
from tests.fakes import FakeTableService

MANAGE_GUILD = 1 << 5
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
ALL_PERMS = MANAGE_GUILD | VIEW_CHANNEL | SEND_MESSAGES


class FakeDiscord:
    def __init__(self, channel: dict | None = None, error: Exception | None = None) -> None:
        self.channel = channel or {
            "id": "111",
            "guild_id": "222",
            "type": 0,
            "name": "テスト",
        }
        self.error = error
        self.requested: list[str] = []

    def get_channel(self, channel_id: str) -> dict:
        self.requested.append(channel_id)
        if self.error:
            raise self.error
        return dict(self.channel, id=channel_id)


class FakeSession:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[tuple[str, dict]] = []

    def patch(self, url, json, timeout):
        self.calls.append((url, dict(json)))
        return type("Response", (), {"status_code": self.status})()


def signed(secret: SigningKey, payload: dict, timestamp: str = "1700000000"):
    body = json.dumps(payload).encode("utf-8")
    message = timestamp.encode("utf-8") + body
    signature = secret.sign(message).signature.hex()
    headers = {
        "X-Signature-Timestamp": timestamp,
        "X-Signature-Ed25519": signature,
    }
    return body, headers


def config(public_key: str) -> CloudConfig:
    return CloudConfig(
        bot_token="bot-token",
        storage_connection="unused",
        client_id="app-id",
        public_key=public_key,
    )


def payload(*, permissions: int = ALL_PERMS, app_permissions: int = ALL_PERMS) -> dict:
    return {
        "type": 2,
        "token": "interaction-token",
        "guild_id": "222",
        "member": {"permissions": str(permissions)},
        "app_permissions": str(app_permissions),
        "data": {
            "name": "setup",
            "options": [{"name": "channel", "value": "111"}],
        },
    }


@pytest.fixture
def secret():
    return SigningKey.generate()


def test_verify_signature(secret):
    body, headers = signed(secret, {"type": 1})
    assert interactions.verify_signature(
        secret.verify_key.encode().hex(),
        headers["X-Signature-Timestamp"],
        body,
        headers["X-Signature-Ed25519"],
    )
    assert not interactions.verify_signature(
        secret.verify_key.encode().hex(),
        headers["X-Signature-Timestamp"],
        body + b"tampered",
        headers["X-Signature-Ed25519"],
    )
    assert not interactions.verify_signature("", "1700000000", body, "ab")


def test_process_deferred_saves_guild_and_returns_message(secret):
    body, headers = signed(secret, payload())
    store = TableStore("", service=FakeTableService())

    token, message = interactions.process_deferred(
        body=body,
        headers=headers,
        config=config(secret.verify_key.encode().hex()),
        store=store,
        discord=FakeDiscord(),
    )

    assert token == "interaction-token"
    assert "✅ 通知先を #テスト" in message
    guilds = store.list_guilds()
    assert len(guilds) == 1
    assert guilds[0].guild_id == "222"
    assert guilds[0].channel_id == "111"
    assert guilds[0].permissions_ok is True


def test_process_deferred_requires_manage_guild(secret):
    body, headers = signed(secret, payload(permissions=VIEW_CHANNEL | SEND_MESSAGES))
    store = TableStore("", service=FakeTableService())
    store.ensure_tables()

    _, message = interactions.process_deferred(
        body=body,
        headers=headers,
        config=config(secret.verify_key.encode().hex()),
        store=store,
        discord=FakeDiscord(),
    )

    assert "MANAGE_GUILD" in message
    assert store.list_guilds() == []


def test_process_deferred_warns_about_missing_send_permission(secret):
    body, headers = signed(
        secret, payload(app_permissions=MANAGE_GUILD | VIEW_CHANNEL)
    )
    store = TableStore("", service=FakeTableService())

    _, message = interactions.process_deferred(
        body=body,
        headers=headers,
        config=config(secret.verify_key.encode().hex()),
        store=store,
        discord=FakeDiscord(),
    )

    assert "✅" in message
    assert "権限が足りません" in message
    assert "メッセージを送信" in message
    assert store.list_guilds()[0].permissions_ok is False


def test_process_deferred_rejects_bad_signature(secret):
    body, _ = signed(secret, payload())
    with pytest.raises(interactions.SignatureError):
        interactions.process_deferred(
            body=body,
            headers={
                "X-Signature-Timestamp": "1700000000",
                "X-Signature-Ed25519": "00" * 64,
            },
            config=config(secret.verify_key.encode().hex()),
        )


def test_process_deferred_returns_error_message_on_storage_failure(secret):
    class BrokenStore:
        def ensure_tables(self):
            raise RuntimeError("table down")

    body, headers = signed(secret, payload())
    token, message = interactions.process_deferred(
        body=body,
        headers=headers,
        config=config(secret.verify_key.encode().hex()),
        store=BrokenStore(),
        discord=FakeDiscord(),
    )
    assert token == "interaction-token"
    assert "設定を保存できませんでした" in message


def test_edit_original_response_patches_webhook():
    session = FakeSession()
    assert interactions.edit_original_response(
        "app-id", "tok", "✅ 完了", session=session
    )
    url, body = session.calls[0]
    assert url == "https://discord.com/api/v10/webhooks/app-id/tok/messages/@original"
    assert body == {"content": "✅ 完了"}


def test_edit_original_response_reports_http_failure():
    session = FakeSession(status=404)
    assert not interactions.edit_original_response(
        "app-id", "tok", "x", session=session
    )
