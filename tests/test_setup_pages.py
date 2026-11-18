"""cloud.setup_pages（OAuth 設定フローのページ）のテスト。"""

from cloud import oauth, setup_pages
from cloud.config import CloudConfig
from cloud.discord import DiscordRestError
from cloud.storage import TableStore
from fakes import FakeTableService
from pso2boost.tz import now_jst


def make_config(**overrides) -> CloudConfig:
    values = dict(
        bot_token="test-token",
        storage_connection="unused",
        host_name="pso2ngs-boost-bot.azurewebsites.net",
        client_id="1572407928101806202",
        client_secret="test-secret",
    )
    values.update(overrides)
    return CloudConfig(**values)


def make_store() -> TableStore:
    store = TableStore("unused", service=FakeTableService())
    store.ensure_tables()
    return store


def session_cookie(store: TableStore, guild_ids: list[str]) -> dict:
    session_id = oauth.make_session_id()
    store.put_session(
        session_id,
        {
            "uid": "42",
            "name": "でんぱ",
            "guilds": guild_ids,
            "exp": oauth.session_expiry(),
        },
        now_jst(),
    )
    return {setup_pages.SESSION_COOKIE: session_id}


# ------------------------------------------------------------------ 1. 開始
def test_start_redirects_to_discord_with_state_cookie():
    page = setup_pages.start(make_config())

    assert page.status == 302
    assert page.headers["Location"].startswith("https://discord.com/oauth2/authorize")
    assert "state=" in page.headers["Location"]
    cookie = page.headers["Set-Cookie"]
    assert cookie.startswith("pso2state=")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    # state の値が URL とクッキーで一致する
    import urllib.parse as parse

    state_in_url = dict(parse.parse_qsl(parse.urlparse(page.headers["Location"]).query))["state"]
    assert f"pso2state={state_in_url}" in cookie


# -------------------------------------------------------------- 2. コールバック
class FakeOAuthSession:
    """oauth.exchange_code / get_user / get_user_guilds の代わり。"""

    def __init__(self, *, user=None, guilds=None) -> None:
        self.user = user or {"id": "42", "username": "でんぱ", "global_name": "でんぱ"}
        self.guilds = guilds or []
        self.calls: list[str] = []

    def post(self, url, data=None, timeout=None):
        self.calls.append(url)
        class R:
            status_code = 200
            text = ""
            def json(self):
                return {"access_token": "at"}
        return R()

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        class R:
            status_code = 200
            text = ""
            def json(self):
                return self_user_or_guilds(url)
        r = R()

        def self_user_or_guilds(u):
            if u.endswith("/users/@me/guilds"):
                return self.guilds
            return self.user

        return r


def test_callback_rejects_state_mismatch():
    config = make_config()
    page = setup_pages.callback(
        config,
        make_store(),
        query={"code": "abc", "state": "wrong"},
        cookies={setup_pages.STATE_COOKIE: "right"},
        session=FakeOAuthSession(),
    )
    assert page.status == 400
    assert "状態の確認" in page.body


def test_callback_lists_manageable_guilds_and_stores_session():
    config = make_config()
    store = make_store()
    fake = FakeOAuthSession(
        guilds=[
            {"id": "111", "name": "BSMG", "permissions": str(oauth.MANAGE_GUILD)},
            {"id": "222", "name": "一般鯖", "permissions": "0"},
        ]
    )

    page = setup_pages.callback(
        config,
        store,
        query={"code": "abc", "state": "right"},
        cookies={setup_pages.STATE_COOKIE: "right"},
        session=fake,
    )

    assert page.status == 200
    assert "BSMG" in page.body
    assert "一般鯖" not in page.body
    assert "guild_id=111" in page.body
    # クッキーは小さな ID だけで、中身は Table のサーバー側セッションにある
    cookie = page.headers["Set-Cookie"]
    assert cookie.startswith(f"{setup_pages.SESSION_COOKIE}=")
    token = cookie.split(";")[0].partition("=")[2]
    assert len(token) <= 64  # クッキー上限に対して余裕がある
    payload = store.get_session(token, now_jst())
    assert payload["guilds"] == ["111"]
    assert payload["name"] == "でんぱ"


def test_callback_with_no_manageable_guilds():
    config = make_config()
    page = setup_pages.callback(
        config,
        make_store(),
        query={"code": "abc", "state": "right"},
        cookies={setup_pages.STATE_COOKIE: "right"},
        session=FakeOAuthSession(guilds=[]),
    )
    assert "設定できるサーバーがありません" in page.body


def test_callback_handles_access_denied():
    page = setup_pages.callback(
        make_config(),
        make_store(),
        query={"error": "access_denied"},
        cookies={},
        session=FakeOAuthSession(),
    )
    assert "キャンセル" in page.body


# ------------------------------------------------------------------ 3. チャンネル
class FakeBot:
    def __init__(self, channels: dict[str, list[dict]], status: int = 200) -> None:
        self.channels = channels
        self.status = status

    def get_guild_channels(self, guild_id: str) -> list[dict]:
        if self.status >= 400:
            raise DiscordRestError(f"HTTP {self.status}", status_code=self.status)
        return self.channels.get(guild_id, [])

    def get_channel(self, channel_id: str) -> dict:
        for channels in self.channels.values():
            for channel in channels:
                if str(channel.get("id")) == str(channel_id):
                    return channel
        raise DiscordRestError("HTTP 404: Unknown Channel", status_code=404)


CHANNELS = {
    "111": [
        {"id": "555", "name": "テスト", "type": 0, "guild_id": "111"},
        {"id": "556", "name": "お知らせ", "type": 5, "guild_id": "111"},
        {"id": "777", "name": "ボイス", "type": 2, "guild_id": "111"},
    ]
}


def test_channels_lists_text_channels_only():
    config = make_config()
    store = make_store()
    page = setup_pages.channels(
        config,
        store,
        query={"guild_id": "111"},
        cookies=session_cookie(store, ["111", "999"]),
        discord=FakeBot(CHANNELS),
    )
    assert page.status == 200
    assert 'value="555"' in page.body and "テスト" in page.body
    assert 'value="556"' in page.body
    assert "777" not in page.body  # ボイスチャンネルは出ない


def test_channels_rejects_guild_without_permission():
    config = make_config()
    store = make_store()
    page = setup_pages.channels(
        config,
        store,
        query={"guild_id": "222"},
        cookies=session_cookie(store, ["111"]),
        discord=FakeBot(CHANNELS),
    )
    assert page.status == 403


def test_channels_shows_invite_when_bot_missing():
    config = make_config()
    store = make_store()
    page = setup_pages.channels(
        config,
        store,
        query={"guild_id": "111"},
        cookies=session_cookie(store, ["111"]),
        discord=FakeBot({}, status=403),
    )
    assert page.status == 200
    assert "Bot がこのサーバーにいません" in page.body
    assert "guild_id=111" in page.body  # サーバー指定つきの招待 URL


def test_channels_rejects_unknown_session():
    page = setup_pages.channels(
        make_config(), make_store(), query={"guild_id": "111"}, cookies={}
    )
    assert page.status == 403


def test_channels_rejects_expired_session():
    config = make_config()
    store = make_store()
    import time

    session_id = oauth.make_session_id()
    store.put_session(
        session_id,
        {"uid": "42", "name": "x", "guilds": ["111"], "exp": int(time.time()) - 1},
        now_jst(),
    )
    page = setup_pages.channels(
        config,
        store,
        query={"guild_id": "111"},
        cookies={setup_pages.SESSION_COOKIE: session_id},
        discord=FakeBot(CHANNELS),
    )
    assert page.status == 403


# ------------------------------------------------------------------ 4. 保存
def test_save_validates_and_upserts():
    config = make_config()
    store = make_store()

    page = setup_pages.save(
        config,
        store,
        form={"guild_id": "111", "channel_id": "555"},
        cookies=session_cookie(store, ["111"]),
        discord=FakeBot(CHANNELS),
    )

    assert page.status == 200
    assert "✅ 通知先を #テスト に設定しました" in page.body
    guilds = {g.guild_id: g for g in store.list_guilds()}
    assert guilds["111"].channel_id == "555"
    assert guilds["111"].enabled is True


def test_save_rejects_other_guild_channel():
    config = make_config()
    store = make_store()
    other = {"999": [{"id": "888", "name": "他所", "type": 0, "guild_id": "999"}]}

    page = setup_pages.save(
        config,
        store,
        form={"guild_id": "111", "channel_id": "888"},
        cookies=session_cookie(store, ["111"]),
        discord=FakeBot({**CHANNELS, **other}),
    )
    assert "サーバー内のチャンネル" in page.body
    assert store.list_guilds() == []


def test_save_rejects_voice_channel():
    config = make_config()
    store = make_store()

    page = setup_pages.save(
        config,
        store,
        form={"guild_id": "111", "channel_id": "777"},
        cookies=session_cookie(store, ["111"]),
        discord=FakeBot(CHANNELS),
    )
    assert "テキストチャンネル" in page.body
    assert store.list_guilds() == []


def test_save_rejects_missing_permission_in_session():
    config = make_config()
    store = make_store()

    page = setup_pages.save(
        config,
        store,
        form={"guild_id": "222", "channel_id": "555"},
        cookies=session_cookie(store, ["111"]),
        discord=FakeBot(CHANNELS),
    )
    assert page.status == 403
    assert store.list_guilds() == []


def test_save_updates_existing_row():
    config = make_config()
    store = make_store()

    setup_pages.save(
        config, store,
        form={"guild_id": "111", "channel_id": "555"},
        cookies=session_cookie(store, ["111"]),
        discord=FakeBot(CHANNELS),
    )
    page = setup_pages.save(
        config, store,
        form={"guild_id": "111", "channel_id": "556"},
        cookies=session_cookie(store, ["111"]),
        discord=FakeBot(CHANNELS),
    )

    assert "✅" in page.body
    guilds = {g.guild_id: g for g in store.list_guilds()}
    assert len(guilds) == 1
    assert guilds["111"].channel_id == "556"
