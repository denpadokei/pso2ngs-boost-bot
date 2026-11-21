"""Cloudflare Worker から転送された deferred interaction のテスト。"""

from __future__ import annotations

import json
import time

import pytest
from nacl.signing import SigningKey

from cloud import interactions
from cloud.config import CloudConfig
from cloud.runner import RunSummary
from cloud.storage import GuildConfig, TableStore
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
        self.roles = [{"id": "222", "permissions": str(VIEW_CHANNEL | SEND_MESSAGES)}]

    def get_channel(self, channel_id: str) -> dict:
        self.requested.append(channel_id)
        if self.error:
            raise self.error
        return dict(self.channel, id=channel_id)

    def get_me(self) -> dict:
        return {"id": "bot-id"}

    def get_guild_member(self, guild_id: str, member_id: str) -> dict:
        return {"roles": []}

    def get_guild_roles(self, guild_id: str) -> list[dict]:
        return self.roles


class FakeSession:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[tuple[str, dict]] = []

    def patch(self, url, json, timeout):
        self.calls.append((url, dict(json)))
        return type("Response", (), {"status_code": self.status})()


def signed(secret: SigningKey, payload: dict, timestamp: str | None = None):
    timestamp = timestamp or str(int(time.time()))
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


def payload(*, permissions: int = ALL_PERMS, app_permissions: int = ALL_PERMS, name="setup") -> dict:
    return {
        "type": 2,
        "token": "interaction-token",
        "guild_id": "222",
        "member": {"permissions": str(permissions)},
        "app_permissions": str(app_permissions),
        "data": {
            "name": name,
            "options": [{"name": "channel", "value": "111"}] if name == "setup" else [],
        },
    }


def sync_payload(**kwargs) -> dict:
    return payload(name="sync", **kwargs)


@pytest.fixture
def secret():
    return SigningKey.generate()


def test_is_valid_public_key():
    assert interactions.is_valid_public_key("ab" * 32) is True
    assert interactions.is_valid_public_key("") is False
    assert interactions.is_valid_public_key("abc") is False
    assert interactions.is_valid_public_key("zz" * 32) is False
    assert interactions.is_valid_public_key("ab" * 31) is False


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


def test_process_deferred_warns_about_selected_channel_permission(secret):
    """呼び出し元ではなく、通知先チャンネルの権限で警告する。"""
    channel = {
        "id": "111",
        "guild_id": "222",
        "type": 0,
        "name": "投稿不可",
        "permission_overwrites": [
            {
                "id": "222",  # @everyone
                "type": 0,
                "deny": str(SEND_MESSAGES),
                "allow": "0",
            }
        ],
    }
    discord = FakeDiscord(channel=channel)
    discord.roles = [{"id": "222", "permissions": str(VIEW_CHANNEL)}]
    # 呼び出し元の app_permissions は十分でも、選択チャンネルは投稿不可
    body, headers = signed(secret, payload(app_permissions=ALL_PERMS))
    store = TableStore("", service=FakeTableService())

    _, message = interactions.process_deferred(
        body=body,
        headers=headers,
        config=config(secret.verify_key.encode().hex()),
        store=store,
        discord=discord,
    )

    assert "✅" in message
    assert "権限が足りません" in message
    assert "メッセージを送信" in message
    assert store.list_guilds()[0].permissions_ok is False


def test_process_deferred_does_not_claim_unverified_permissions(secret):
    class NoPermissionsDiscord(FakeDiscord):
        def get_me(self):
            raise RuntimeError("temporary Discord failure")

    body, headers = signed(secret, payload())
    store = TableStore("", service=FakeTableService())

    _, message = interactions.process_deferred(
        body=body,
        headers=headers,
        config=config(secret.verify_key.encode().hex()),
        store=store,
        discord=NoPermissionsDiscord(),
    )

    assert "権限を確認できませんでした" in message
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


def test_process_deferred_rejects_stale_signature(secret):
    body, headers = signed(secret, payload(), timestamp=str(int(time.time()) - 3600))
    with pytest.raises(interactions.SignatureError):
        interactions.process_deferred(
            body=body,
            headers=headers,
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


def test_sync_requires_manage_guild():
    message = interactions.handle_application_command(
        sync_payload(permissions=VIEW_CHANNEL | SEND_MESSAGES),
        config=config("ab" * 32),
    )

    assert "MANAGE_GUILD" in message


def test_sync_requires_setup_first():
    store = TableStore("", service=FakeTableService())
    store.ensure_tables()

    message = interactions.handle_application_command(
        sync_payload(), config=config("ab" * 32), store=store
    )

    assert "未設定" in message


def test_sync_runs_and_reports_summary():
    store = TableStore("", service=FakeTableService())
    store.ensure_tables()
    store.upsert_guild(GuildConfig(guild_id="222", channel_id="111"))
    calls: list[dict] = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return RunSummary(guilds=1, notified=2, created=1, status="ok")

    message = interactions.handle_application_command(
        sync_payload(), config=config("ab" * 32), store=store, sync_run=fake_run
    )

    assert "✅ 同期が完了しました" in message
    assert "通知 2" in message and "作成 1" in message
    assert calls and calls[0] == {}


def test_sync_degraded_includes_notes():
    store = TableStore("", service=FakeTableService())
    store.ensure_tables()
    store.upsert_guild(GuildConfig(guild_id="222", channel_id="111"))

    def fake_run(**kwargs):
        return RunSummary(
            guilds=1, status="partial", actions=["PSO2の日の情報が既定値です"]
        )

    message = interactions.handle_application_command(
        sync_payload(), config=config("ab" * 32), store=store, sync_run=fake_run
    )

    assert "⚠️ 同期に問題がありました" in message
    assert "既定値" in message


def test_sync_failure_does_not_leak_exception():
    store = TableStore("", service=FakeTableService())
    store.ensure_tables()
    store.upsert_guild(GuildConfig(guild_id="222", channel_id="111"))

    def boom(**kwargs):
        raise RuntimeError("secret-token-value")

    message = interactions.handle_application_command(
        sync_payload(), config=config("ab" * 32), store=store, sync_run=boom
    )

    assert "同期に失敗" in message
    assert "secret-token-value" not in message


def test_process_deferred_dispatches_sync(secret):
    body, headers = signed(secret, sync_payload())
    store = TableStore("", service=FakeTableService())
    store.ensure_tables()
    store.upsert_guild(GuildConfig(guild_id="222", channel_id="111"))

    def fake_run(**kwargs):
        return RunSummary(guilds=1, notified=1, status="ok")

    token, message = interactions.process_deferred(
        body=body,
        headers=headers,
        config=config(secret.verify_key.encode().hex()),
        store=store,
        sync_run=fake_run,
    )

    assert token == "interaction-token"
    assert "同期が完了しました" in message
