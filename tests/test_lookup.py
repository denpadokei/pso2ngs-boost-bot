import pytest

from pso2boost.lookup import DiscordLookup, DiscordLookupError


class FakeResponse:
    def __init__(self, status: int, payload) -> None:
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.headers: dict[str, str] = {}
        self.requested: list[str] = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        for suffix, response in self.responses.items():
            if url.endswith(suffix):
                return response
        return FakeResponse(404, {"message": "Not Found"})


def make_lookup(responses) -> DiscordLookup:
    return DiscordLookup("token", session=FakeSession(responses))


def test_resolves_single_guild():
    lookup = make_lookup({"/users/@me/guilds": FakeResponse(200, [{"id": "1", "name": "テスト"}] )})
    assert lookup.resolve_guild_id() == "1"


def test_multiple_guilds_requires_explicit_id():
    lookup = make_lookup(
        {
            "/users/@me/guilds": FakeResponse(
                200, [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]
            )
        }
    )
    with pytest.raises(DiscordLookupError) as excinfo:
        lookup.resolve_guild_id()
    assert "DISCORD_GUILD_ID" in str(excinfo.value)
    assert lookup.resolve_guild_id("2") == "2"


def test_no_guilds_is_an_error():
    lookup = make_lookup({"/users/@me/guilds": FakeResponse(200, [])})
    with pytest.raises(DiscordLookupError):
        lookup.resolve_guild_id()


def test_resolves_channel_by_name():
    lookup = make_lookup(
        {
            "/guilds/1/channels": FakeResponse(
                200,
                [
                    {"id": "10", "name": "general", "type": 0},
                    {"id": "11", "name": "テスト", "type": 0},
                    {"id": "12", "name": "ボイス", "type": 2},
                ],
            )
        }
    )
    assert lookup.resolve_channel_id("1", name="テスト") == "11"
    assert lookup.resolve_channel_id("1", name="#テスト") == "11"


def test_channel_not_found_lists_candidates():
    lookup = make_lookup(
        {"/guilds/1/channels": FakeResponse(200, [{"id": "10", "name": "general", "type": 0}])}
    )
    with pytest.raises(DiscordLookupError) as excinfo:
        lookup.resolve_channel_id("1", name="missing")
    assert "#general" in str(excinfo.value)


def test_invalid_token_is_reported():
    lookup = make_lookup({"/users/@me/guilds": FakeResponse(401, {"message": "401: Unauthorized"})})
    with pytest.raises(DiscordLookupError) as excinfo:
        lookup.guilds()
    assert "トークン" in str(excinfo.value)
