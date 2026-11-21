from cloud.commands import (
    ALL_COMMANDS,
    MANAGE_GUILD,
    SETUP_COMMAND,
    SYNC_COMMAND,
    commands_url,
    register_commands,
)


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, list[dict]]] = []

    def put(self, url, json, timeout):
        self.calls.append((url, json))
        return type(
            "Response",
            (),
            {
                "status_code": 200,
                "json": lambda self: [
                    {"name": command["name"], "id": command["name"]}
                    for command in json
                ],
            },
        )()


def test_setup_and_sync_are_registered():
    assert {command["name"] for command in ALL_COMMANDS} == {"setup", "sync"}
    assert SETUP_COMMAND in ALL_COMMANDS
    assert SYNC_COMMAND["default_member_permissions"] == str(MANAGE_GUILD)
    assert SYNC_COMMAND["dm_permission"] is False
    assert SYNC_COMMAND["options"] == []


def test_register_commands_sends_both():
    session = FakeSession()

    result = register_commands("token", "app-id", session=session)

    assert [item["name"] for item in result] == ["setup", "sync"]
    url, payload = session.calls[0]
    assert url == commands_url("app-id")
    assert [command["name"] for command in payload] == ["setup", "sync"]
    assert session.headers["Authorization"] == "Bot token"


def test_dev_guild_url():
    assert commands_url("app", "guild").endswith("/applications/app/guilds/guild/commands")
