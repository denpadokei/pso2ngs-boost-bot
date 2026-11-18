"""cloud.interactions（署名検証・PING・/setup deferReply 版）のテスト。"""

import json
from datetime import datetime

from nacl.signing import SigningKey

from cloud.config import CloudConfig
from cloud.interactions import (
    MANAGE_GUILD,
    deferred_ephemeral,
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


class FakePatchSession:
    """followup の PATCH を記録する Fake。"""

    def __init__(self, status: int = 200) -> None:
        self.patches: list[tuple[str, dict]] = []
        self.status = status

    def patch(self, url, json=None, timeout=None):
        self.patches.append((url, json))
        class R:
            status_code = self.status
            text = ""
        return R()

    @property
    def last_content(self) -> str:
        return self.patches[-1][1]["content"] if self.patches else ""


class FakeDiscord:
    def __init__(self, channels: dict[str, dict]) -> None:
        self.channels = channels

    def get_channel(self, channel_id: str) -> dict:
        if channel_id not in self.channels:
            from cloud.interactions import DiscordRestError

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
    patch_session: FakePatchSession | None = None,
):
    """署名して送り、followup は同期的に実行する（テスト用）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    signature = signing_key.sign(TIMESTAMP.encode() + body).signature
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
        followup_runner=(lambda fn: fn()) if patch_session is not None else None,
        patch_session=patch_session,
    )


def make_payload(**overrides) -> dict:
    payload = {
        "type": 2,
        "application_id": "1572407928101806202",
        "token": "inter-action-token",
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
    _, other_public = make_signing()
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
def test_setup_returns_deferred_ack_and_patches_success():
    signing, public_hex = make_signing()
    store = make_store()
    patch = FakePatchSession()

    status, body = signed_call(
        public_hex,
        signing,
        make_payload(),
        store=store,
        discord=FakeDiscord(CHANNELS),
        patch_session=patch,
    )

    # 即時応答は type 5（deferReply・ephemeral）
    assert status == 200
    assert body == {"type": 5, "data": {"flags": 64}}

    # followup が @original を PATCH している
    assert len(patch.patches) == 1
    url, payload = patch.patches[0]
    assert url.endswith(
        "/webhooks/1572407928101806202/inter-action-token/messages/@original"
    )
    assert "✅ 通知先を #テスト に設定しました" in patch.last_content
    assert "毎週水曜 13:00" in patch.last_content

    guilds = {g.guild_id: g for g in store.list_guilds()}
    assert guilds["111"].channel_id == "555"
    assert guilds["111"].permissions_ok is True


def test_setup_accepts_announcement_channel():
    signing, public_hex = make_signing()
    store = make_store()
    patch = FakePatchSession()

    payload = make_payload()
    payload["data"]["options"][0]["value"] = "556"
    status, body = signed_call(
        public_hex, signing, payload, store=store, discord=FakeDiscord(CHANNELS),
        patch_session=patch,
    )

    assert body["type"] == 5
    assert "#お知らせ" in patch.last_content
    assert {g.guild_id: g for g in store.list_guilds()}["111"].channel_id == "556"


def test_setup_rejects_member_without_manage_guild_immediately():
    signing, public_hex = make_signing()
    store = make_store()
    patch = FakePatchSession()

    payload = make_payload(member={"permissions": "0"})
    status, body = signed_call(
        public_hex, signing, payload, store=store, discord=FakeDiscord(CHANNELS),
        patch_session=patch,
    )

    # 権限なしはローカル判定なので即時 type 4 で拒否
    assert status == 200
    assert body["type"] == 4
    assert "MANAGE_GUILD" in body["data"]["content"]
    assert patch.patches == []
    assert store.list_guilds() == []


def test_setup_rejects_voice_channel_via_followup():
    signing, public_hex = make_signing()
    store = make_store()
    patch = FakePatchSession()

    payload = make_payload()
    payload["data"]["options"][0]["value"] = "777"
    status, body = signed_call(
        public_hex, signing, payload, store=store, discord=FakeDiscord(CHANNELS),
        patch_session=patch,
    )

    assert body["type"] == 5
    assert "テキストチャンネル" in patch.last_content
    assert store.list_guilds() == []


def test_setup_rejects_channel_of_other_guild_via_followup():
    signing, public_hex = make_signing()
    store = make_store()
    patch = FakePatchSession()

    payload = make_payload()
    payload["data"]["options"][0]["value"] = "888"
    status, body = signed_call(
        public_hex, signing, payload, store=store, discord=FakeDiscord(CHANNELS),
        patch_session=patch,
    )

    assert "サーバー内のチャンネル" in patch.last_content
    assert store.list_guilds() == []


def test_setup_rejects_missing_channel_immediately():
    signing, public_hex = make_signing()
    payload = make_payload(data={"name": "setup", "options": []})

    status, body = signed_call(
        public_hex, signing, payload, store=make_store(), discord=FakeDiscord(CHANNELS),
        patch_session=FakePatchSession(),
    )
    assert body["type"] == 4
    assert "チャンネルを指定" in body["data"]["content"]


def test_setup_warns_when_bot_permissions_missing():
    signing, public_hex = make_signing()
    store = make_store()
    patch = FakePatchSession()

    payload = make_payload(app_permissions="0")
    status, body = signed_call(
        public_hex, signing, payload, store=store, discord=FakeDiscord(CHANNELS),
        patch_session=patch,
    )

    assert "権限が足りません" in patch.last_content
    guilds = {g.guild_id: g for g in store.list_guilds()}
    assert guilds["111"].permissions_ok is False
    assert guilds["111"].channel_id == "555"


def test_setup_updates_existing_guild_row():
    signing, public_hex = make_signing()
    store = make_store()
    discord = FakeDiscord(CHANNELS)

    signed_call(
        public_hex, signing, make_payload(), store=store, discord=discord,
        patch_session=FakePatchSession(),
    )

    payload = make_payload()
    payload["data"]["options"][0]["value"] = "556"
    signed_call(
        public_hex, signing, payload, store=store, discord=discord,
        patch_session=FakePatchSession(),
    )

    guilds = {g.guild_id: g for g in store.list_guilds()}
    assert len(guilds) == 1
    assert guilds["111"].channel_id == "556"


def test_setup_creates_missing_tables():
    signing, public_hex = make_signing()
    store = TableStore("unused", service=FakeTableService())  # ensure していない

    status, body = signed_call(
        public_hex, signing, make_payload(), store=store, discord=FakeDiscord(CHANNELS),
        patch_session=FakePatchSession(),
    )
    assert status == 200
    assert {g.guild_id: g for g in store.list_guilds()}["111"].channel_id == "555"


def test_setup_reports_storage_failure_via_followup():
    from fakes import FakeTableService

    class BrokenTableService(FakeTableService):
        def upsert_entity(self, name, entity):
            raise RuntimeError("table unavailable")

    signing, public_hex = make_signing()
    store = TableStore("unused", service=BrokenTableService())
    store.ensure_tables()
    patch = FakePatchSession()

    status, body = signed_call(
        public_hex, signing, make_payload(), store=store, discord=FakeDiscord(CHANNELS),
        patch_session=patch,
    )

    # ACK 自体は出る。失敗は followup の PATCH で届く
    assert status == 200
    assert body["type"] == 5
    assert "設定を保存できませんでした" in patch.last_content


def test_unknown_command_returns_400():
    signing, public_hex = make_signing()
    payload = make_payload(data={"name": "status", "options": []})

    status, _ = signed_call(
        public_hex, signing, payload, store=make_store(), discord=FakeDiscord(CHANNELS),
        patch_session=FakePatchSession(),
    )
    assert status == 400


def test_ephemeral_and_deferred_shape():
    assert ephemeral("こんにちは") == {
        "type": 4,
        "data": {"flags": 64, "content": "こんにちは"},
    }
    assert deferred_ephemeral() == {"type": 5, "data": {"flags": 64}}
