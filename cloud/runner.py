"""週次バッチ。PSO2 のデータを1回だけ取得し、サーバーごとに同期する。

処理順（docs/shared-bot.md §6）:
1. Discord イベントの作成・更新・削除（sync_events）
2. チャンネルへの投稿（plan_actions の結果を送信）
3. 投稿に成功したものだけフラグを保存（失敗は次回の再試行に残す）

PSO2 側（pso2.jp）への HTTP リクエストは、準備フェーズをメインスレッドで
順番に実行し、Fetcher のキャッシュで1回に抑える。
並列実行はスレッドプール（requests が同期 API のため、設計書の
asyncio.Semaphore の代わりに max_workers を同時数の上限として使う）。
"""

from __future__ import annotations

import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import requests

from pso2boost.discover import discover_from_rss
from pso2boost.discord_events import ScheduledEventsApi
from pso2boost.fetch import Fetcher, SCHEDULE_URL, FetchError
from pso2boost.maintenance import MaintenanceCalendar, fetch_calendar
from pso2boost.models import BoostEvent, merge_events
from pso2boost.notify import DiscordNotifier
from pso2boost.pso2day import discover_pso2_day
from pso2boost.schedule import DEFAULT_KEYWORDS, filter_relevant, parse_schedule_html
from pso2boost.service import PlannedAction, enrich_event, plan_actions
from pso2boost.state import State
from pso2boost.sync import sync_events
from pso2boost.tz import now_jst

from .config import CloudConfig
from .discord import DiscordRest, DiscordRestError
from .snapshot import decode_events, encode_events
from .storage import GuildConfig, LAST_ERROR_MAX, SHARED_PARTITION, TableStore

log = logging.getLogger(__name__)

# /sync は週次実行のスナップショットだけを使う。更新は週次が担うため古さは問わない。
OFFLINE_SNAPSHOT_MAX_AGE = 30 * 24 * 3600


@dataclass
class GuildTarget:
    """1回のバッチで処理するサーバー。"""

    guild_id: str
    channel_id: str
    mention_role_id: str = ""


@dataclass
class GuildWork:
    """準備フェーズで作る、1サーバー分の作業内容。"""

    target: GuildTarget
    state: State
    events: list[BoostEvent]
    actions: list[PlannedAction]


@dataclass
class GuildResult:
    guild_id: str
    channel_id: str = ""
    notified: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    errors: int = 0
    last_error: str = ""

    @property
    def ok(self) -> bool:
        return self.errors == 0


@dataclass
class RunSummary:
    guilds: int = 0
    notified: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    failed: int = 0
    status: str = "ok"  # ok / skipped / partial / failed
    details: list[GuildResult] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"サーバー {self.guilds} / 通知 {self.notified} / "
            f"イベント作成 {self.created} / 更新 {self.updated} / "
            f"削除 {self.deleted} / 失敗 {self.failed}"
        )


def fetch_schedule(
    fetcher: Fetcher,
    now: datetime,
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS,
) -> tuple[MaintenanceCalendar, list[BoostEvent]]:
    """カレンダーとスケジュールを1回だけ取得・パースする（全サーバー共通）。"""
    calendar = fetch_calendar(fetcher, now)
    events = parse_schedule_html(
        fetcher.get(SCHEDULE_URL), now, keywords, calendar=calendar
    )
    return calendar, events


def discover_events(
    fetcher: Fetcher,
    shared_state: State,
    now: datetime,
    schedule_events: list[BoostEvent],
    calendar: MaintenanceCalendar,
) -> tuple[list[BoostEvent], bool]:
    """RSS から新着記事を拾い、スケジュールと合わせたイベント一覧を作る。

    RSS の既読は全サーバー共有（_shared パーティション）なので、
    pso2.jp への取得はサーバー数によらず1回で済む。

    戻り値の bool は PSO2の日が実データ（既定値ではない）を読めたかどうか。
    """
    rss_events = discover_from_rss(fetcher, shared_state, now, calendar=calendar)
    pso2day = discover_pso2_day(fetcher, now)
    events = filter_relevant(
        merge_events(merge_events(schedule_events, rss_events), pso2day.events), now
    )
    return events, pso2day.ok


def prepare_guild(
    target: GuildTarget,
    *,
    events: list[BoostEvent],
    now: datetime,
    store: TableStore,
) -> GuildWork:
    """メインスレッドで実行する準備: state の読み出しと、通知計画の作成。"""
    state = store.load_state(target.guild_id, now)
    return GuildWork(target, state, events, plan_actions(events, state, now))


def process_guild(
    work: GuildWork,
    *,
    config: CloudConfig,
    now: datetime,
    store: TableStore,
    api: ScheduledEventsApi | None = None,
    notifier: DiscordNotifier | None = None,
) -> GuildResult:
    """1サーバー分の同期（ワーカースレッドで実行）。失敗しても例外を外へ出さない。"""
    target = work.target
    result = GuildResult(guild_id=target.guild_id, channel_id=target.channel_id)

    api = api or ScheduledEventsApi(config.bot_token, target.guild_id)
    sync_result = sync_events(work.events, work.state, api, now)
    result.created = sync_result.created
    result.updated = sync_result.updated
    result.deleted = sync_result.deleted
    result.errors += sync_result.errors
    if sync_result.errors:
        detail = " / ".join(sync_result.error_messages[:2])
        result.last_error = f"Discord イベントの同期に失敗: {detail}"[:LAST_ERROR_MAX]

    notifier = notifier or _make_notifier(config, target)
    for action in work.actions:
        sent = notifier.send_event(
            action.event,
            action.kind,
            sync_result.links.get(action.event.key),
            now=now,
        )
        if not sent:
            result.errors += 1
            disabled_reason = getattr(notifier, "disabled_reason", "") or ""
            status = getattr(notifier, "last_status", None)
            if disabled_reason:
                result.last_error = f"{action.kind} の投稿先が未設定です（{disabled_reason}）"
            elif status in (403, 404):
                result.last_error = (
                    f"{action.kind} の投稿が拒否されました (HTTP {status}): "
                    "チャンネルの投稿権限を確認してください"
                )
                try:
                    store.set_permissions_ok(target.guild_id, False)
                except Exception:
                    log.warning("permissions_ok の更新に失敗: %s", target.guild_id)
            else:
                detail = f" (HTTP {status})" if status else ""
                result.last_error = f"{action.kind} 通知の送信に失敗{detail}"
            continue
        result.notified += 1
        # 投稿に成功したものだけフラグを立てる（失敗は次回再試行）
        if action.kind == "new":
            work.state.mark_seen(action.event)
            work.state.mark_sent("new", action.event.key)
        else:
            work.state.mark_sent(action.kind, action.event.key)

        # 送信成功をできるだけ早く永続化し、保存失敗時の重複リスクを減らす
        try:
            store.save_state(target.guild_id, work.state)
        except Exception as exc:
            log.error("通知後の状態保存に失敗 (%s): %s", target.guild_id, exc)
            result.errors += 1
            result.last_error = f"通知後の状態保存に失敗（次回重複の可能性）: {exc}"

    work.state.prune(days=config.state_retention_days)
    try:
        store.save_state(target.guild_id, work.state)
    except Exception as exc:
        log.error("最終状態の保存に失敗 (%s): %s", target.guild_id, exc)
        result.errors += 1
        result.last_error = f"最終状態の保存に失敗（次回重複の可能性）: {exc}"
    return result


def _make_notifier(config: CloudConfig, target: GuildTarget) -> DiscordNotifier:
    return DiscordNotifier(
        bot_token=config.bot_token,
        channel_id=target.channel_id,
        mention_role_id=target.mention_role_id or None,
        style=config.notify_style,
    )


def resolve_targets(config: CloudConfig, store: TableStore) -> list[GuildTarget]:
    """対象サーバーを決める。Table の登録を優先し、なければ環境変数へフォールバック。"""
    guilds: list[GuildConfig] = [
        guild for guild in store.list_guilds() if guild.enabled and guild.channel_id
    ]
    for guild in store.list_guilds():
        if guild.enabled and not guild.channel_id:
            log.warning("サーバー %s は通知先チャンネルが未設定のため対象外", guild.guild_id)
    if guilds:
        return [
            GuildTarget(guild.guild_id, guild.channel_id, guild.mention_role_id)
            for guild in guilds
        ]
    if config.guild_id and config.channel_id:
        # Phase 1: 環境変数による1サーバー運用
        return [GuildTarget(config.guild_id, config.channel_id)]
    return []


def filter_available_targets(
    targets: list[GuildTarget],
    config: CloudConfig,
    store: TableStore,
    now: datetime,
    *,
    guild_client: DiscordRest | None = None,
) -> list[GuildTarget]:
    """Bot がキックされたサーバー（GET /guilds が 404/403）を停止して対象から外す。

    一時的なエラー（429 / 5xx / ネットワーク）では無効化しない。
    """
    if not targets:
        return targets
    discord = guild_client or DiscordRest(config.bot_token)
    available: list[GuildTarget] = []
    for target in targets:
        try:
            discord.get_guild(target.guild_id)
            available.append(target)
        except DiscordRestError as exc:
            if exc.status_code in (403, 404):
                store.disable_guild(
                    target.guild_id, now, f"サーバーにアクセスできません: {exc}"
                )
                log.warning(
                    "サーバー %s を無効化しました（Bot が抜けた可能性）: %s",
                    target.guild_id,
                    exc,
                )
            else:
                log.warning(
                    "サーバー %s の確認に失敗（無効化せず続行）: %s", target.guild_id, exc
                )
                available.append(target)
        except requests.RequestException as exc:
            log.warning(
                "サーバー %s の確認に失敗（無効化せず続行）: %s", target.guild_id, exc
            )
            available.append(target)
    return available


def run_weekly(
    config: CloudConfig,
    *,
    now: datetime | None = None,
    store: TableStore | None = None,
    fetcher: Fetcher | None = None,
    guild_client: DiscordRest | None = None,
    api_factory: Callable[[GuildTarget], ScheduledEventsApi] | None = None,
    notifier_factory: Callable[[GuildTarget], DiscordNotifier] | None = None,
    guild_ids: list[str] | None = None,
    snapshot_ttl: int = 600,
    offline: bool = False,
) -> RunSummary:
    """Timer から呼ばれる週次バッチの入口。

    排他ロックを取得できた実行だけが処理を行い、1サーバーの失敗は
    GuildResult に閉じ込めて他サーバーへ影響させない。

    ``guild_ids`` を渡すと対象サーバーをそのIDに限定する（``/sync`` 用）。

    ``snapshot_ttl`` 秒以内に取得したイベント一覧があれば再利用する。

    ``offline=True`` のときは pso2.jp へ**一切アクセスしない**。週次実行が保存した
    スナップショットを（古さに関わらず）適用するだけ。スナップショットが無ければ
    スキップする。``/sync`` はこのモードで呼ぶ。
    """
    now = now or now_jst()
    summary = RunSummary()
    degraded = False  # 情報源が既定値に退化した（同期自体は続行）

    if not config.bot_token:
        summary.actions.append("DISCORD_BOT_TOKEN が未設定のため何もしません")
        summary.status = "skipped"
        log.error("DISCORD_BOT_TOKEN が未設定です")
        return summary

    store = store or TableStore(config.storage_connection)
    store.ensure_tables()
    lock_owner = secrets.token_hex(8)
    if not store.try_acquire_lock(now, lock_owner):
        summary.actions.append("別の同期が実行中です（ロック取得失敗）")
        summary.status = "skipped"
        log.warning("別の同期が実行中のためスキップします")
        return summary

    try:
        # 掃除はおまけ。失敗しても本処理は止めない
        try:
            store.prune_sessions(now)
        except Exception as exc:
            log.warning("セッションの掃除に失敗しました（同期は続行）: %s", exc)

        targets = resolve_targets(config, store)
        # Bot がキックされたサーバーは無効化して対象から外す（Phase 3）
        targets = filter_available_targets(
            targets, config, store, now, guild_client=guild_client
        )
        if guild_ids is not None:
            wanted = set(guild_ids)
            targets = [target for target in targets if target.guild_id in wanted]
        summary.guilds = len(targets)
        if not targets:
            summary.actions.append("対象サーバーがありません（設定ページで登録してください）")
            summary.status = "skipped"
            log.warning("対象サーバーがありません")
            return summary

        fresh = False
        try:
            if offline:
                # /sync はサイトへアクセスせず、週次実行の保存データだけを使う。
                # 古さは問わない（更新は週次実行が担う）。
                snapshot = store.load_event_snapshot(now, OFFLINE_SNAPSHOT_MAX_AGE)
                if snapshot is None:
                    message = "同期できるデータがありません。週次実行後に再度お試しください"
                    summary.actions.append(message)
                    summary.status = "skipped"
                    log.warning(message)
                    return summary
                events = decode_events(snapshot)
                calendar = MaintenanceCalendar()
                log.info("保存済みスナップショットを適用: %d 件", len(events))
            else:
                snapshot = store.load_event_snapshot(now, snapshot_ttl)
                if snapshot is not None:
                    # 直近の取得結果を再利用
                    events = decode_events(snapshot)
                    calendar = MaintenanceCalendar()
                    log.info("共有スナップショットを再利用: %d 件", len(events))
                else:
                    fetcher = fetcher or Fetcher(delay=config.delay)
                    calendar, schedule_events = fetch_schedule(
                        fetcher, now, config.keywords
                    )
                    # RSS の既読は全サーバー共有。ここで pso2.jp への取得は完了する
                    shared_state = store.load_state(SHARED_PARTITION, now)
                    events, pso2day_ok = discover_events(
                        fetcher, shared_state, now, schedule_events, calendar
                    )
                    if not pso2day_ok:
                        message = "PSO2の日の告知記事を取得・解析できず既定値で同期します"
                        summary.actions.append(message)
                        log.warning(message)
                        degraded = True
                    store.save_state(SHARED_PARTITION, shared_state)
                    fresh = True
        except FetchError as exc:
            message = f"PSO2 データの取得に失敗: {exc}"
            summary.actions.append(message)
            summary.status = "failed"
            log.error(message)
            return summary
        log.info("イベント解析: %d 件", len(events))

        # 準備は直列（PSO2 への HTTP を1回に抑える）
        works: list[GuildWork] = []
        prepare_failures: list[GuildResult] = []
        for target in targets:
            try:
                works.append(
                    prepare_guild(target, events=events, now=now, store=store)
                )
            except Exception as exc:
                log.exception("サーバー %s の準備に失敗", target.guild_id)
                prepare_failures.append(
                    GuildResult(
                        guild_id=target.guild_id,
                        channel_id=target.channel_id,
                        errors=1,
                        last_error=f"{type(exc).__name__}: {exc}"[:LAST_ERROR_MAX],
                    )
                )
        # 記事本文の取得もメインスレッドで1回だけ（イベントは全サーバー共通のオブジェクト）
        # スナップショット再利用時は詳細取得済みなので、サイトへ再アクセスしない
        enriched: set[str] = set()
        if fresh:
            for work in works:
                for action in work.actions:
                    if action.event.key not in enriched:
                        enrich_event(action.event, fetcher, now)
                        enriched.add(action.event.key)
            try:
                store.save_event_snapshot(encode_events(events), now)
            except Exception as exc:
                log.warning("イベントスナップショットの保存に失敗（次回は再取得）: %s", exc)

        def run_one(work: GuildWork) -> GuildResult:
            try:
                return process_guild(
                    work,
                    config=config,
                    now=now,
                    store=store,
                    api=api_factory(work.target) if api_factory else None,
                    notifier=notifier_factory(work.target) if notifier_factory else None,
                )
            except Exception as exc:
                # 契約: 1サーバーの失敗で全体を止めない
                log.exception("サーバー %s の処理で例外", work.target.guild_id)
                return GuildResult(
                    guild_id=work.target.guild_id,
                    channel_id=work.target.channel_id,
                    errors=1,
                    last_error=f"{type(exc).__name__}: {exc}"[:LAST_ERROR_MAX],
                )

        workers = max(1, min(config.concurrency, len(works)))
        if workers == 1:
            results = [run_one(work) for work in works]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(run_one, works))

        # 準備失敗も含めて結果を記録する
        all_results = results + prepare_failures
        for result in all_results:
            try:
                store.record_guild_run(
                    result.guild_id, now,
                    last_error=None if result.ok else result.last_error,
                )
            except Exception as exc:
                log.error("実行結果の保存に失敗 (%s): %s", result.guild_id, exc)
            if result.ok:
                log.info(
                    "サーバー %s: 通知 %d / 作成 %d / 更新 %d / 削除 %d",
                    result.guild_id, result.notified, result.created,
                    result.updated, result.deleted,
                )
            else:
                summary.failed += 1
                log.error(
                    "サーバー %s で失敗: %s（他のサーバーは続行）",
                    result.guild_id, result.last_error,
                )

        summary.notified = sum(result.notified for result in all_results)
        summary.created = sum(result.created for result in all_results)
        summary.updated = sum(result.updated for result in all_results)
        summary.deleted = sum(result.deleted for result in all_results)
        summary.details = all_results
        summary.status = "partial" if (summary.failed or degraded) else "ok"
        log.info("週次同期完了: %s", summary.summary)
        return summary
    finally:
        store.release_lock(lock_owner)
