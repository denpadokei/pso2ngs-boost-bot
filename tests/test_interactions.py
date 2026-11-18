"""cloud.interactions（署名検証・PING・/setup）のテスト。"""

import json
from datetime import datetime

from nacl.signing import SigningKey

from cloud.commands import SETUP_COMMAND, commands_url, register_commands
from cloud.config import CloudConfig
from cloud.interactions import (
    MANAGE_GUILD,
    DiscordRestError,
    ephemeral,
    handle_interaction,
    verify_signature,
)
from cloud.storage import TableStore
from fakes import FakeTableService
from pso2boost.discord_events import RECOMMENDED_PERMISSIONS
from pso2boost.tz import JST

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=JST)
TIMESTAMP = "1794960000"


def make_signing() -> tuple[SigningKey, str]:
    key = SigningKey.generate()
    return key, key.verify_key.encode().hex()


def make_config(public_key: str) -> CloudConfig:
    return CloudConfig(
        bot_token="test-token",
        storage_connection="unused",
        public_key=public_key,
    )


def make_store() -> TableStore:
    store = TableStore("unused", service=FakeTableService())
    store.ensure_tables()
    return store


def make_bare_store() -> TableStore:
    """テーブル未作成の FakeTableService をそのまま使う（ensure しない）。"""
    return TableStore("unused", service=FakeTableService())


class FakeDiscord:
    def __init__(self, channels: dict[str, dict]) -> None:
        self.channels = channels

    def get_channel(self, channel_id: str) -> dict:
        if channel_id not in self.channels:
            raise DiscordRestError(f"HTTP 404: Unknown Channel {channel_id}")
        return dict(self.channels[channel_id])


def signed_call(
    verify_hex: str,
    signing_key: SigningKey,
    payload: dict,
    *,
    config: CloudConfig | None = None,
    store: TableStore | None = None,
    discord: FakeDiscord | None = None,
    tamper: bool = False,
):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    signature = signing_key.sign(TIMESTAMP.encode() + body).signature
    if tamper:
        body = body + b" "
    headers = {
        "X-Signature-Ed25519": signature.hex(),
        "X-Signature-Timestamp": TIMESTAMP,
    }
    return handle_interaction(
        body=body,
        headers=headers,
        config=config or make_config(verify_hex),
        store=store,
        discord=discord,
    )


def make_payload(**overrides) -> dict:
    payload = {
        "type": 2,
        "guild_id": "111",
        "data": {
            "name": "setup",
            "options": [{"name": "channel", "value": "555"}],
        },
        "member": {"permissions": str(MANAGE_GUILD)},
        "app_permissions": str(RECOMMENDED_PERMISSIONS),
    }
    payload.update(overrides)
    return payload


CHANNELS = {
    "555": {"id": "555", "name": "テスト", "type": 0, "guild_id": "111"},
    "556": {"id": "556", "name": "お知らせ", "type": 5, "guild_id": "111"},
    "777": {"id": "777", "name": "ボイス", "type": 2, "guild_id": "111"},
    "888": {"id": "888", "name": "他所", "type": 0, "guild_id": "999"},
}


# ------------------------------------------------------------------ 署名検証
def test_verify_signature_accepts_valid():
    signing, public_hex = make_signing()
    body = b'{"type":1}'

    signature = signing.sign(TIMESTAMP.encode() + body).signature
    assert verify_signature(public_hex, TIMESTAMP, body, signature.hex())


def test_verify_signature_rejects_tampered_body():
    signing, public_hex = make_signing()
    body = b'{"type":1}'

    signature = signing.sign(TIMESTAMP.encode() + body).signature
    assert not verify_signature(public_hex, TIMESTAMP, b'{"type":2}', signature.hex())


def test_verify_signature_rejects_garbage():
    _, public_hex = make_signing()
    assert not verify_signature(public_hex, TIMESTAMP, b"x", "zz")
    assert not verify_signature("", TIMESTAMP, b"x", "00")
    assert not verify_signature(public_hex, "", b"x", "00")


# ---------------------------------------------------------------------- PING
def test_ping_returns_type_1():
    signing, public_hex = make_signing()
    status, body = signed_call(
        public_hex, signing, {"type": 1}, store=make_store(), discord=FakeDiscord(CHANNELS)
    )
    assert status == 200
    assert body == {"type": 1}


def test_invalid_signature_returns_401():
    signer, _ = make_signing()
    _, other_public = make_signing()  # 署名鍵とは別のアプリの公開鍵
    status, body = signed_call(
        other_public, signer, make_payload(), store=make_store(), discord=FakeDiscord(CHANNELS)
    )
    assert status == 401


def test_missing_public_key_returns_503():
    status, body = handle_interaction(
        body=b"{}", headers={}, config=CloudConfig(bot_token="t")
    )
    assert status == 503


# ------------------------------------------------------------------- /setup
def test_setup_saves_channel_and_replies_ephemeral():
    signing, public_hex = make_signing()
    store = make_store()

    status, body = signed_call(
        public_hex,
        signing,
        make_payload(),
        store=store,
        discord=FakeDiscord(CHANNELS),
    )

    assert status == 200
    assert body["type"] == 4
    assert body["data"]["flags"] == 64  # ephemeral
    assert "#テスト" in body["data"]["content"]
    assert "毎週水曜 13:00" in body["data"]["content"]

    guilds = {g.guild_id: g for g in store.list_guilds()}
    assert guilds["111"].channel_id == "555"
    assert guilds["111"].enabled is True
    assert guilds["111"].permissions_ok is True


def test_setup_accepts_announcement_channel():
    signing, public_hex = make_signing()
    store = make_store()

    payload = make_payload()
    payload["data"]["options"][0]["value"] = "556"
    status, body = signed_call(
        public_hex, signing, payload, store=store, discord=FakeDiscord(CHANNELS)
    )

    assert status == 200
    assert "#お知らせ" in body["data"]["content"]
    assert {g.guild_id: g for g in store.list_guilds()}["111"].channel_id == "556"


def test_setup_rejects_member_without_manage_guild():
    signing, public_hex = make_signing()
    store = make_store()

    payload = make_payload(member={"permissions": "0"})
    status, body = signed_call(
        public_hex, signing, payload, store=store, discord=FakeDiscord(CHANNELS)
    )

    assert status == 200
    assert "MANAGE_GUILD" in body["data"]["content"]
    assert store.list_guilds() == []


def test_setup_rejects_voice_channel():
    signing, public_hex = make_signing()
    store = make_store()

    payload = make_payload()
    payload["data"]["options"][0]["value"] = "777"
    status, body = signed_call(
        public_hex, signing, payload, store=store, discord=FakeDiscord(CHANNELS)
    )

    assert status == 200
    assert "テキストチャンネル" in body["data"]["content"]
    assert store.list_guilds() == []


def test_setup_rejects_channel_of_other_guild():
    signing, public_hex = make_signing()
    store = make_store()

    payload = make_payload()
    payload["data"]["options"][0]["value"] = "888"
    status, body = signed_call(
        public_hex, signing, payload, store=store, discord=FakeDiscord(CHANNELS)
    )

    assert "サーバー内のチャンネル" in body["data"]["content"]
    assert store.list_guilds() == []


def test_setup_rejects_missing_channel():
    signing, public_hex = make_signing()
    payload = make_payload(data={"name": "setup", "options": []})

    status, body = signed_call(
        public_hex, signing, payload, store=make_store(), discord=FakeDiscord(CHANNELS)
    )
    assert "チャンネルを指定" in body["data"]["content"]


def test_setup_warns_but_saves_when_bot_permissions_missing():
    signing, public_hex = make_signing()
    store = make_store()

    payload = make_payload(app_permissions="0")
    status, body = signed_call(
        public_hex, signing, payload, store=store, discord=FakeDiscord(CHANNELS)
    )

    assert "権限が足りません" in body["data"]["content"]
    guilds = {g.guild_id: g for g in store.list_guilds()}
    assert guilds["111"].permissions_ok is False
    assert guilds["111"].channel_id == "555"


def test_setup_updates_existing_guild_row():
    signing, public_hex = make_signing()
    store = make_store()
    discord = FakeDiscord(CHANNELS)

    signed_call(public_hex, signing, make_payload(), store=store, discord=discord)

    payload = make_payload()
    payload["data"]["options"][0]["value"] = "556"
    status, body = signed_call(public_hex, signing, payload, store=store, discord=discord)

    guilds = {g.guild_id: g for g in store.list_guilds()}
    assert len(guilds) == 1
    assert guilds["111"].channel_id == "556"


def test_unknown_command_returns_400():
    signing, public_hex = make_signing()
    payload = make_payload(data={"name": "status", "options": []})

    status, _ = signed_call(
        public_hex, signing, payload, store=make_store(), discord=FakeDiscord(CHANNELS)
    )
    assert status == 400


def test_ephemeral_shape():
    body = ephemeral("こんにちは")
    assert body == {"type": 4, "data": {"flags": 64, "content": "こんにちは"}}


def test_setup_creates_missing_tables():
    """初回 Timer 実行前（テーブル未作成）でも /setup が成功する。"""
    signing, public_hex = make_signing()
    store = make_bare_store()  # ensure していない＝テーブルがまだ無い状態

    status, body = signed_call(
        public_hex, signing, make_payload(), store=store, discord=FakeDiscord(CHANNELS)
    )

    assert status == 200
    assert "✅" in body["data"]["content"]
    guilds = {g.guild_id: g for g in store.list_guilds()}
    assert guilds["111"].channel_id == "555"


def test_setup_replies_ephemeral_on_storage_failure():
    """テーブル障害でも 3 秒以内に ephemeral 応答を返す（タイムアウトさせない）。"""
    from fakes import FakeTableService

    class BrokenTableService(FakeTableService):
        def upsert_entity(self, name, entity):
            raise RuntimeError("table unavailable")

    signing, public_hex = make_signing()
    store = TableStore("unused", service=BrokenTableService())
    store.ensure_tables()

    status, body = signed_call(
        public_hex, signing, make_payload(), store=store, discord=FakeDiscord(CHANNELS)
    )

    assert status == 200
    assert body["type"] == 4
    assert body["data"]["flags"] == 64
    assert "設定を保存できませんでした" in body["data"]["content"]


# ------------------------------------------------------------ コマンド登録
def test_setup_command_definition():
    assert SETUP_COMMAND["name"] == "setup"
    assert SETUP_COMMAND["default_member_permissions"] == str(MANAGE_GUILD)
    option = SETUP_COMMAND["options"][0]
    assert option["type"] == 7
    assert option["required"] is True
    assert option["channel_types"] == [0, 5]


def test_commands_url_global_and_guild():
    assert commands_url("app").endswith("/applications/app/commands")
    assert commands_url("app", "123").endswith("/applications/app/guilds/123/commands")


def test_register_commands_puts_setup():
    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return [{"id": "cmd1"}]

    session_records = {}

    class FakeSession:
        headers = {}

        def put(self, url, json=None, timeout=None):
            session_records["url"] = url
            session_records["payload"] = json
            return FakeResponse()

    result = register_commands("token", "app", dev_guild_id="123", session=FakeSession())

    assert result == [{"id": "cmd1"}]
    assert session_records["url"].endswith("/applications/app/guilds/123/commands")
    assert session_records["payload"] == [SETUP_COMMAND]
