"""ブースト予定と Discord のスケジュールイベントを同期する。

- 新規: 期間が未来ならイベントを作成
- 変更: 名前・期間・場所・説明が変わったら更新
- 終了: 終了から一定日数たったら削除（履歴を残しすぎない）

すでに開始済みのキャンペーンは、Discord 側の開始時刻を偽装できないため作成しない。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .discord_events import DiscordApiError, ScheduledEventsApi
from .models import BoostEvent
from .state import State

log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 7


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: int = 0
    links: dict[str, str] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return (
            f"作成 {self.created} / 更新 {self.updated} / "
            f"削除 {self.deleted} / スキップ {self.skipped}"
        )


def build_payload(event: BoostEvent, now: datetime) -> tuple[dict, str] | None:
    """Discord に送る内容と、変更検知用の署名。作成不要なら None。"""
    start = event.period.start.at
    end = event.period.end.at
    if start is None or end is None:
        return None
    if end <= now:
        return None
    if start <= now:
        # 開始済み: Discord 側で開始時刻を過去にはできない
        return None

    description_lines = [f"・{value}" for value in event.buffs_all]
    if event.buffs_premium:
        description_lines.append("プレミアムセット有効期間中:")
        description_lines += [f"・{value}" for value in event.buffs_premium]
    description_lines.append(f"開催期間: {event.period.display()}")
    if event.url:
        description_lines.append(f"告知: {event.url}")

    payload = {
        "name": event.label,
        "description": "\n".join(description_lines),
        "start": start,
        "end": end,
        "location": event.location,
    }
    signature = hashlib.sha1(
        json.dumps(
            {**payload, "start": start.isoformat(), "end": end.isoformat()},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    return payload, signature


def sync_events(
    events: list[BoostEvent],
    state: State,
    api: ScheduledEventsApi,
    now: datetime,
    *,
    dry_run: bool = False,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> SyncResult:
    result = SyncResult()

    for event in events:
        entry = state.discord_event(event.key)
        end = event.period.end.at

        # 終了してから日が経ったものは削除
        if entry and end is not None and end < now - timedelta(days=retention_days):
            if dry_run:
                log.info("[dry-run] イベント削除: %s", event.label)
            else:
                try:
                    api.delete(entry["id"])
                except DiscordApiError as exc:
                    log.error("イベント削除に失敗 (%s): %s", event.label, exc)
                    result.errors += 1
                    continue
                state.remove_discord_event(event.key)
            result.deleted += 1
            continue

        built = build_payload(event, now)
        if built is None:
            if entry is None:
                result.skipped += 1
            else:
                result.links[event.key] = api.event_url(entry["id"])
            continue

        payload, signature = built

        if entry is None:
            if dry_run:
                log.info(
                    "[dry-run] イベント作成: %s (%s ～ %s)",
                    payload["name"],
                    payload["start"],
                    payload["end"],
                )
                result.created += 1
                continue
            try:
                event_id = api.create(**payload)
            except DiscordApiError as exc:
                log.error("イベント作成に失敗 (%s): %s", event.label, exc)
                result.errors += 1
                continue
            state.set_discord_event(
                event.key,
                {
                    "id": event_id,
                    "signature": signature,
                    "name": event.label,
                    "end": payload["end"].isoformat(),
                },
            )
            result.created += 1
            result.links[event.key] = api.event_url(event_id)
            log.info("Discord イベントを作成: %s", event.label)
            continue

        result.links[event.key] = api.event_url(entry["id"])
        if entry.get("signature") == signature:
            continue

        if dry_run:
            log.info("[dry-run] イベント更新: %s", payload["name"])
            result.updated += 1
            continue
        try:
            api.update(entry["id"], **payload)
        except DiscordApiError as exc:
            if "HTTP 404" not in str(exc):
                log.error("イベント更新に失敗 (%s): %s", event.label, exc)
                result.errors += 1
                continue
            # 別サーバーの状態を引き継いだ場合など、Discord 側に存在しない
            log.warning("Discord 側にイベントが無いため作り直します: %s", event.label)
            state.remove_discord_event(event.key)
            try:
                new_id = api.create(**payload)
            except DiscordApiError as create_exc:
                log.error("イベント作成に失敗 (%s): %s", event.label, create_exc)
                result.errors += 1
                continue
            state.set_discord_event(
                event.key,
                {
                    "id": new_id,
                    "signature": signature,
                    "name": event.label,
                    "end": payload["end"].isoformat(),
                },
            )
            result.created += 1
            result.links[event.key] = api.event_url(new_id)
            continue
        state.set_discord_event(
            event.key,
            {
                "id": entry["id"],
                "signature": signature,
                "name": event.label,
                "end": payload["end"].isoformat(),
            },
        )
        result.updated += 1
        log.info("Discord イベントを更新: %s", event.label)

    _delete_stale(state, api, now, retention_days, result, dry_run)
    return result


def _delete_stale(
    state: State,
    api: ScheduledEventsApi,
    now: datetime,
    retention_days: int,
    result: SyncResult,
    dry_run: bool,
) -> None:
    """一覧から消えた（＝スケジュールから外れた）古いイベントを片付ける。"""
    for key, entry in state.discord_events().items():
        raw_end = entry.get("end")
        if not raw_end:
            continue
        try:
            end = datetime.fromisoformat(raw_end)
        except (TypeError, ValueError):
            continue
        if end.tzinfo is None:
            continue
        if end >= now - timedelta(days=retention_days):
            continue
        if dry_run:
            log.info("[dry-run] イベント削除（終了済み）: %s", entry.get("name") or key)
            result.deleted += 1
            continue
        try:
            api.delete(entry["id"])
        except DiscordApiError as exc:
            if "HTTP 404" in str(exc):
                state.remove_discord_event(key)
                continue
            log.error("イベント削除に失敗 (%s): %s", entry.get("name") or key, exc)
            result.errors += 1
            continue
        state.remove_discord_event(key)
        result.deleted += 1
