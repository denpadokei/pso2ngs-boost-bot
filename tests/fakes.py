"""テスト共通の Fake。

azure-data-tables や Discord API の代わりに、cloud/ のコードが使う
インターフェースだけを模倣する。
"""

from __future__ import annotations

import re

from pso2boost.fetch import FetchError


class FakeTableService:
    """azure.data.tables のうち TableStore が使う部分だけのインメモリ実装。"""

    def __init__(self) -> None:
        self.tables: dict[str, dict[tuple[str, str], dict]] = {}
        self.deleted: list[tuple[str, str, str]] = []

    def table_exists(self, name: str) -> bool:
        return name in self.tables

    def create_table(self, name: str) -> None:
        self.tables.setdefault(name, {})

    def _entities(self, name: str) -> dict[tuple[str, str], dict]:
        if name not in self.tables:
            raise KeyError(f"テーブルがありません: {name}")
        return self.tables[name]

    def list_entities(self, name: str, query: str | None = None) -> list[dict]:
        entities = list(self._entities(name).values())
        if query:
            match = re.fullmatch(r"PartitionKey eq '(.*)'", query)
            assert match, f"未対応のクエリ: {query}"
            partition = match.group(1)
            entities = [e for e in entities if e.get("PartitionKey") == partition]
        return [dict(entity) for entity in entities]

    def upsert_entity(self, name: str, entity: dict) -> None:
        entities = self._entities(name)
        key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
        existing = entities.get(key)
        if existing is None:
            entities[key] = dict(entity)
        else:
            existing.update(entity)  # merge

    def delete_entity(self, name: str, partition_key: str, row_key: str) -> None:
        entities = self._entities(name)
        entities.pop((str(partition_key), str(row_key)), None)
        self.deleted.append((name, str(partition_key), str(row_key)))


class FakeApi:
    """ScheduledEventsApi の代わり。作成・更新・削除を記録するだけ。"""

    def __init__(self, guild_id: str) -> None:
        self.guild_id = guild_id
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self._counter = 0

    def event_url(self, event_id: str) -> str:
        return f"https://discord.com/events/{self.guild_id}/{event_id}"

    def create(self, **kwargs) -> str:
        self._counter += 1
        self.created.append(kwargs)
        return f"ev{self.guild_id}-{self._counter}"

    def update(self, event_id: str, **kwargs) -> None:
        self.updated.append((event_id, kwargs))

    def delete(self, event_id: str) -> None:
        self.deleted.append(event_id)


class FakeNotifier:
    """DiscordNotifier の代わり。チャンネルごとに送信内容を記録する。"""

    def __init__(self, channel_id: str, fail: bool = False) -> None:
        self.channel_id = channel_id
        self.sent: list[tuple[str, str, str | None]] = []
        self.fail = fail

    def send_event(self, event, kind, event_url=None, now=None) -> bool:
        if self.fail:
            return False
        self.sent.append((kind, event.key, event_url))
        return True


class FakeFetcher:
    """Fetcher の代わり。指定 URL だけ返し、それ以外は失敗にする。

    実物と同じく、取得に成功した URL はキャッシュし、``cache=True`` なら
    再取得せずに返す。
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.requested: list[str] = []
        self._cache: dict[str, str] = {}

    def get(self, url: str, cache: bool = False) -> str:
        if cache and url in self._cache:
            return self._cache[url]
        self.requested.append(url)
        if url not in self.mapping:
            raise FetchError(f"not in fixture: {url}")
        text = self.mapping[url]
        self._cache[url] = text
        return text
