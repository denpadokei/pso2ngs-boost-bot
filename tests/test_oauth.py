"""cloud.oauth（Discord OAuth2 設定フローのコア）のテスト。"""

from urllib.parse import parse_qs, urlparse

import pytest

from cloud import oauth


# ------------------------------------------------------------------ 認可 URL
def test_authorize_url_contains_scope_and_state():
    url = oauth.authorize_url("123", "https://example.com/api/setup/callback", "abcdef")
    parts = urlparse(url)
    query = parse_qs(parts.query)

    assert parts.netloc == "discord.com"
    assert parts.path == "/oauth2/authorize"
    assert query["client_id"] == ["123"]
    assert query["redirect_uri"] == ["https://example.com/api/setup/callback"]
    assert query["scope"] == [oauth.SETUP_SCOPES]
    assert oauth.SETUP_SCOPES == "identify guilds"
    assert query["state"] == ["abcdef"]
    assert query["response_type"] == ["code"]


def test_make_state_is_random_and_long():
    first = oauth.make_state()
    second = oauth.make_state()
    assert len(first) >= 16
    assert first != second


# ------------------------------------------------------------------ 権限フィルタ
def test_manageable_guilds_keeps_only_manage_guild():
    guilds = [
        {"id": "1", "name": "管理者", "permissions": str(oauth.MANAGE_GUILD)},
        {"id": "2", "name": "一般", "permissions": "0"},
        {"id": "3", "name": "複合", "permissions": str((1 << 13) | oauth.MANAGE_GUILD)},
        {"id": "4", "name": "不正", "permissions": "not-a-number"},
    ]
    result = oauth.manageable_guilds(guilds)
    assert [g["id"] for g in result] == ["1", "3"]


# ------------------------------------------------------------------ 署名セッション
def test_issue_and_load_session_roundtrip():
    token = oauth.issue_session(
        "s3cret", user_id="42", username="でんぱ", guild_ids=["111", "222"]
    )
    payload = oauth.load_session("s3cret", token)

    assert payload["uid"] == "42"
    assert payload["name"] == "でんぱ"
    assert payload["guilds"] == ["111", "222"]
    assert payload["exp"] > 0


def test_load_session_rejects_wrong_secret():
    token = oauth.issue_session("s3cret", user_id="42", username="x", guild_ids=["1"])
    assert oauth.load_session("other", token) is None


def test_load_session_rejects_tampered_payload():
    import base64

    token = oauth.issue_session("s3cret", user_id="42", username="x", guild_ids=["1"])
    body, signature = token.split(".")
    forged = base64.urlsafe_b64encode(
        b'{"uid":"999","guilds":["777"],"exp":9999999999}'
    ).decode("ascii")
    assert oauth.load_session("s3cret", f"{forged}.{signature}") is None


def test_load_session_rejects_expired():
    token = oauth.issue_session(
        "s3cret",
        user_id="42",
        username="x",
        guild_ids=["1"],
        now=1000.0 - oauth.SESSION_TTL_SECONDS - 1,
    )
    assert oauth.load_session("s3cret", token) is None


def test_load_session_rejects_garbage():
    assert oauth.load_session("s3cret", "") is None
    assert oauth.load_session("s3cret", "not-a-token") is None
    assert oauth.load_session("s3cret", "!!!.???") is None


# ------------------------------------------------------------------ コード交換
class FakeResponse:
    def __init__(self, status: int, payload=None) -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict | None]] = []

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST " + url, data))
        return self.responses.pop(0)

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        return self.responses.pop(0)


def test_exchange_code_posts_authorization_code_grant():
    session = FakeSession([FakeResponse(200, {"access_token": "at"})])
    token = oauth.exchange_code(
        session,
        client_id="123",
        client_secret="shh",
        code="abc",
        redirect_uri="https://example.com/api/setup/callback",
    )
    url, data = session.calls[0]
    assert url.endswith("/api/oauth2/token")
    assert data["grant_type"] == "authorization_code"
    assert data["client_id"] == "123"
    assert data["client_secret"] == "shh"
    assert data["code"] == "abc"
    assert token == {"access_token": "at"}


def test_exchange_code_raises_on_error():
    session = FakeSession([FakeResponse(400, {"error": "invalid_grant"})])
    with pytest.raises(oauth.OAuthError):
        oauth.exchange_code(
            session, client_id="1", client_secret="s", code="bad",
            redirect_uri="https://example.com/cb",
        )


def test_get_user_and_guilds_use_bearer_token():
    session = FakeSession(
        [
            FakeResponse(200, {"id": "42", "username": "denpa"}),
            FakeResponse(200, [{"id": "1", "permissions": "32"}]),
        ]
    )
    user = oauth.get_user(session, "at")
    guilds = oauth.get_user_guilds(session, "at")

    assert user["id"] == "42"
    assert session.calls[0][1]["Authorization"] == "Bearer at"
    assert guilds[0]["id"] == "1"
    assert session.calls[1][1]["Authorization"] == "Bearer at"
