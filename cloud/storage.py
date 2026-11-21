"""Azure Table Storage へのアクセス（guilds / state の2テーブル）。

- ``guilds`` テーブル: PartitionKey="guild", RowKey=guild_id … 登録されたサーバー
- ``state``  テーブル: PartitionKey=guild_id … pso2boost の State を行ごとに保存

変換（``state_to_rows`` / ``rows_to_state`` など）は純粋関数として切り出し、
azure-data-tables SDK なしでテストできるようにしている。
SDK を使うのは ``TableStore`` だけで、import も遅延する。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, unquote

from pso2boost.state import State

log = logging.getLogger(__name__)

GUILD_PARTITION = "guild"
SHARED_PARTITION = "_shared"  # 全サーバー共有の RSS 既読などを置く予約パーティション
SESSION_PARTITION = "_session"  # 設定フローのサーバー側セッション
LOCK_PARTITION = "_lock"  # 週次バッチの排他ロック
LOCK_ROW = "weekly"
LOCK_TTL_SECONDS = 10 * 60
EVENT_ROW_PREFIX = "event:"
RSS_ROW_PREFIX = "rss:"

LAST_ERROR_MAX = 200


class LockConflict(RuntimeError):
    """排他ロックが既に取得されている。"""


def _expiry(row: dict) -> int:
    """セッション/ロック行の exp を安全に読む（壊れていれば期限切れ扱い）。"""
    try:
        return int(row.get("exp") or 0)
    except (TypeError, ValueError):
        log.warning("期限時刻を解釈できない行があります: %s", row.get("RowKey"))
        return 0


@dataclass
class GuildConfig:
    """guilds テーブルの1行（1サーバー分の設定）。"""

    guild_id: str
    channel_id: str
    mention_role_id: str = ""
    enabled: bool = True
    permissions_ok: bool = True
    added_at: str = ""
    last_run_at: str = ""
    last_error: str = ""


def guild_to_row(guild: GuildConfig) -> dict:
    return {
        "PartitionKey": GUILD_PARTITION,
        "RowKey": guild.guild_id,
        "channel_id": guild.channel_id,
        "mention_role_id": guild.mention_role_id,
        "enabled": guild.enabled,
        "permissions_ok": guild.permissions_ok,
        "added_at": guild.added_at,
        "last_run_at": guild.last_run_at,
        "last_error": guild.last_error[:LAST_ERROR_MAX],
    }


def row_to_guild(row: dict) -> GuildConfig:
    return GuildConfig(
        guild_id=str(row.get("RowKey", "")),
        channel_id=str(row.get("channel_id", "") or ""),
        mention_role_id=str(row.get("mention_role_id", "") or ""),
        enabled=bool(row.get("enabled", True)),
        permissions_ok=bool(row.get("permissions_ok", True)),
        added_at=str(row.get("added_at", "") or ""),
        last_run_at=str(row.get("last_run_at", "") or ""),
        last_error=str(row.get("last_error", "") or ""),
    )


# --------------------------------------------------------------------------
# State（pso2boost.state.State）⇔ Table 行の変換
# --------------------------------------------------------------------------
def state_to_rows(guild_id: str, state: State) -> list[dict]:
    """State を state テーブルの行へ変換する。

    1イベント1行（RowKey="event:<key>"）に、seen / sent / discord の情報をまとめる。
    RSS の既読は RowKey="rss:<URL>"（URL は RowKey に使えない文字を含むため quote）。
    """
    data = state.snapshot()
    rows: dict[str, dict] = {}

    def event_row(key: str) -> dict:
        return rows.setdefault(
            EVENT_ROW_PREFIX + key,
            {
                "PartitionKey": guild_id,
                "RowKey": EVENT_ROW_PREFIX + key,
                "updated_at": state.now.isoformat(),
            },
        )

    for key, entry in data["seen"].items():
        event_row(key).update(
            {
                "label": str(entry.get("label", "") or ""),
                "period": str(entry.get("period", "") or ""),
                "url": str(entry.get("url", "") or ""),
                "first_seen": str(entry.get("first_seen", "") or ""),
                "last_seen": str(entry.get("last_seen", "") or ""),
            }
        )

    for sent_key, timestamp in data["sent"].items():
        kind, _, key = sent_key.partition(":")
        if kind in {"new", "start", "end"}:
            event_row(key)[f"{kind}_sent"] = str(timestamp)

    for key, entry in data["discord"].items():
        row = event_row(key)
        row["discord_event_id"] = str(entry.get("id", "") or "")
        row["signature"] = str(entry.get("signature", "") or "")
        row["end"] = str(entry.get("end", "") or "")
        row["discord_name"] = str(entry.get("name", "") or "")

    for url, timestamp in data["rss"].items():
        rows[RSS_ROW_PREFIX + quote(url, safe="")] = {
            "PartitionKey": guild_id,
            "RowKey": RSS_ROW_PREFIX + quote(url, safe=""),
            "updated": str(timestamp),
        }

    return list(rows.values())


def rows_to_state(rows: list[dict], now: datetime) -> State:
    """state テーブルの行から State を復元する。"""
    seen: dict[str, dict] = {}
    sent: dict[str, str] = {}
    rss: dict[str, str] = {}
    discord: dict[str, dict] = {}

    for row in rows:
        row_key = str(row.get("RowKey", ""))
        if row_key.startswith(EVENT_ROW_PREFIX):
            key = row_key[len(EVENT_ROW_PREFIX) :]

            first_seen = str(row.get("first_seen", "") or "") or str(
                row.get("new_sent", "") or ""
            )
            if first_seen:
                seen[key] = {
                    "label": str(row.get("label", "") or ""),
                    "period": str(row.get("period", "") or ""),
                    "url": str(row.get("url", "") or ""),
                    "first_seen": first_seen,
                    "last_seen": str(row.get("last_seen", "") or "")
                    or str(row.get("updated_at", "") or ""),
                }
            for kind in ("new", "start", "end"):
                timestamp = str(row.get(f"{kind}_sent", "") or "")
                if timestamp:
                    sent[f"{kind}:{key}"] = timestamp

            event_id = str(row.get("discord_event_id", "") or "")
            if event_id:
                discord[key] = {
                    "id": event_id,
                    "signature": str(row.get("signature", "") or ""),
                    "name": str(row.get("discord_name", "") or ""),
                    "end": str(row.get("end", "") or ""),
                    "updated": str(row.get("updated_at", "") or ""),
                }
        elif row_key.startswith(RSS_ROW_PREFIX):
            url = unquote(row_key[len(RSS_ROW_PREFIX) :])
            rss[url] = str(row.get("updated", "") or "")

    return State(None, now, data={"seen": seen, "sent": sent, "rss": rss, "discord": discord})


# --------------------------------------------------------------------------
# azure SDK のクライアントを TableStore の IF へ合わせるアダプタ
# --------------------------------------------------------------------------
class _TableServiceAdapter:
    """azure.data.tables.TableServiceClient を TableStore が使う
    インターフェース（table_exists / create_table / list_entities /
    upsert_entity / delete_entity）へ合わせる薄いラッパー。

    SDK の TableServiceClient はテーブル単位の操作を持たず、
    get_table_client() で取得した TableClient を介する必要がある。
    （実機で AttributeError になったため、この違いをここに集約する。
    テストはこの IF を持つ Fake を使うので、SDK 側の表面は
    test_storage.py の契約テストで保証する。）
    """

    def __init__(self, client) -> None:
        self._client = client  # azure.data.tables.TableServiceClient

    def table_exists(self, name: str) -> bool:
        for table in self._client.list_tables():
            if table.name == name:
                return True
        return False

    def create_table(self, name: str) -> None:
        from azure.core.exceptions import ResourceExistsError

        try:
            self._client.create_table(name)
        except ResourceExistsError:  # ensure の競合は無視してよい
            pass

    def list_entities(self, name: str, query: str | None = None) -> list[dict]:
        table = self._client.get_table_client(name)
        # query_entities の第1引数は OData フィルタ（None なら全件）
        return [dict(entity) for entity in table.query_entities(query)]

    def upsert_entity(self, name: str, entity: dict) -> None:
        # 既定の UpdateMode.MERGE なので、部分更新でも他の列は消えない
        self._client.get_table_client(name).upsert_entity(entity)

    def create_entity(self, name: str, entity: dict) -> None:
        from azure.core.exceptions import ResourceExistsError

        try:
            self._client.get_table_client(name).create_entity(entity)
        except ResourceExistsError as exc:  # 競合は LockConflict に変換
            raise LockConflict("entity already exists") from exc

    def delete_entity(self, name: str, partition_key: str, row_key: str) -> None:
        self._client.get_table_client(name).delete_entity(
            partition_key=partition_key, row_key=row_key
        )


# --------------------------------------------------------------------------
# Table へのアクセス
# --------------------------------------------------------------------------
class TableStore:
    """guilds / state の2テーブルを操作する。

    テストでは同じインターフェースを持つ Fake を ``service`` に渡す。
    """

    def __init__(
        self,
        connection_string: str,
        *,
        service=None,
        state_table: str = "state",
        guilds_table: str = "guilds",
    ) -> None:
        self.state_table = state_table
        self.guilds_table = guilds_table
        if service is None:
            from azure.data.tables import TableServiceClient

            service = _TableServiceAdapter(
                TableServiceClient.from_connection_string(connection_string)
            )
        self._service = service

    # ------------------------------------------------------------------ init
    def ensure_tables(self) -> None:
        for name in (self.guilds_table, self.state_table):
            if not self._service.table_exists(name):
                self._service.create_table(name)

    # ---------------------------------------------------------------- guilds
    def list_guilds(self) -> list[GuildConfig]:
        rows = self._service.list_entities(self.guilds_table)
        return [
            row_to_guild(row)
            for row in rows
            if str(row.get("PartitionKey", "")) == GUILD_PARTITION
        ]

    def upsert_guild(self, guild: GuildConfig) -> None:
        self._service.upsert_entity(self.guilds_table, guild_to_row(guild))

    def record_guild_run(
        self, guild_id: str, now: datetime, last_error: str | None = None
    ) -> None:
        """last_run_at を更新し、last_error を記録（None ならクリア）する。"""
        existing = {g.guild_id for g in self.list_guilds()}
        if guild_id not in existing:
            # 環境変数フォールバックのサーバーなど、未登録のものは行を作らない
            return
        self._service.upsert_entity(
            self.guilds_table,
            {
                "PartitionKey": GUILD_PARTITION,
                "RowKey": guild_id,
                "last_run_at": now.isoformat(),
                "last_error": (last_error or "")[:LAST_ERROR_MAX],
            },
        )

    def set_permissions_ok(self, guild_id: str, ok: bool) -> None:
        """実行時に判明した投稿権限の状態を更新する（MERGE で他列は保持）。"""
        if guild_id in {g.guild_id for g in self.list_guilds()}:
            self._service.upsert_entity(
                self.guilds_table,
                {"PartitionKey": GUILD_PARTITION, "RowKey": guild_id, "permissions_ok": bool(ok)},
            )

    def disable_guild(self, guild_id: str, now: datetime, reason: str) -> None:
        """Bot がキックされるなどしたサーバーを停止する（未登録なら何もしない）。

        /setup をやり直せば（upsert で enabled=True に戻る）再度有効化できる。
        """
        if guild_id not in {guild.guild_id for guild in self.list_guilds()}:
            return
        self._service.upsert_entity(
            self.guilds_table,
            {
                "PartitionKey": GUILD_PARTITION,
                "RowKey": guild_id,
                "enabled": False,
                "last_error": reason[:LAST_ERROR_MAX],
                "last_run_at": now.isoformat(),
            },
        )

    # -------------------------------------------------------------- sessions
    def put_session(self, session_id: str, payload: dict, now: datetime) -> None:
        """設定フローのサーバー側セッションを保存する（クッキーはIDだけ）。"""
        self._service.upsert_entity(
            self.state_table,
            {
                "PartitionKey": SESSION_PARTITION,
                "RowKey": session_id,
                "uid": str(payload.get("uid", "")),
                "name": str(payload.get("name", "")),
                "guilds": json.dumps(
                    [str(guild) for guild in payload.get("guilds", [])]
                ),
                "exp": int(payload.get("exp") or 0),
            },
        )

    def get_session(self, session_id: str, now: datetime) -> dict | None:
        """有効なセッションを返す。期限切れは削除して None。"""
        if not session_id:
            return None
        rows = self._service.list_entities(
            self.state_table, query=f"PartitionKey eq '{SESSION_PARTITION}'"
        )
        for row in rows:
            if str(row.get("RowKey", "")) != session_id:
                continue
            if _expiry(row) < int(now.timestamp()):
                self._service.delete_entity(self.state_table, SESSION_PARTITION, session_id)
                return None
            try:
                guilds = json.loads(row.get("guilds") or "[]")
            except (TypeError, json.JSONDecodeError):
                log.warning("セッションのサーバー一覧が壊れています: %s", session_id)
                self._service.delete_entity(self.state_table, SESSION_PARTITION, session_id)
                return None
            return {
                "uid": str(row.get("uid", "")),
                "name": str(row.get("name", "")),
                "guilds": [str(guild) for guild in guilds],
                "exp": _expiry(row),
            }
        return None

    def delete_session(self, session_id: str) -> None:
        if session_id:
            self._service.delete_entity(self.state_table, SESSION_PARTITION, session_id)

    def prune_sessions(self, now: datetime) -> int:
        """期限切れのセッションを掃除する（週次バッチからも呼ぶ）。"""
        rows = self._service.list_entities(
            self.state_table, query=f"PartitionKey eq '{SESSION_PARTITION}'"
        )
        removed = 0
        for row in rows:
            if _expiry(row) < int(now.timestamp()):
                self._service.delete_entity(
                    self.state_table, SESSION_PARTITION, str(row.get("RowKey", ""))
                )
                removed += 1
        return removed

    # ------------------------------------------------------------------ lock
    def try_acquire_lock(self, now: datetime, owner: str) -> bool:
        """週次バッチの排他ロックを取得する（TTL失効分は奪う）。"""
        entity = {
            "PartitionKey": LOCK_PARTITION,
            "RowKey": LOCK_ROW,
            "owner": owner,
            "exp": int(now.timestamp()) + LOCK_TTL_SECONDS,
        }
        try:
            self._service.create_entity(self.state_table, entity)
            return True
        except LockConflict:
            rows = self._service.list_entities(
                self.state_table, query=f"PartitionKey eq '{LOCK_PARTITION}'"
            )
            existing = next(
                (row for row in rows if str(row.get("RowKey")) == LOCK_ROW), None
            )
            if existing is not None and _expiry(existing) < int(now.timestamp()):
                self._service.delete_entity(self.state_table, LOCK_PARTITION, LOCK_ROW)
                try:
                    self._service.create_entity(self.state_table, entity)
                    return True
                except LockConflict:
                    return False
            return False

    def release_lock(self, owner: str) -> None:
        """自分のロックだけを解放する（TTL奪取後に他人のを消さない）。"""
        try:
            rows = self._service.list_entities(
                self.state_table, query=f"PartitionKey eq '{LOCK_PARTITION}'"
            )
            existing = next(
                (row for row in rows if str(row.get("RowKey")) == LOCK_ROW), None
            )
            if existing is None or str(existing.get("owner") or "") != owner:
                return
            self._service.delete_entity(self.state_table, LOCK_PARTITION, LOCK_ROW)
        except Exception:  # TTL で失効するため、解放失敗は致命的ではない
            log.warning("ロックの解放に失敗しました（TTLで失効します）")

    # ----------------------------------------------------------------- state
    def load_state(self, guild_id: str, now: datetime) -> State:
        rows = self._service.list_entities(
            self.state_table, query=f"PartitionKey eq '{guild_id}'"
        )
        return rows_to_state(list(rows), now)

    def save_state(self, guild_id: str, state: State) -> None:
        rows = state_to_rows(guild_id, state)
        existing = {
            str(row.get("RowKey", ""))
            for row in self._service.list_entities(
                self.state_table, query=f"PartitionKey eq '{guild_id}'"
            )
        }
        for row in rows:
            self._service.upsert_entity(self.state_table, row)
        # state.prune() で消えた行を掃除する（終了後の残骸を放置しない）
        for stale in existing - {str(row["RowKey"]) for row in rows}:
            self._service.delete_entity(self.state_table, guild_id, stale)
