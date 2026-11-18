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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import requests

from pso2boost.discover import discover_from_rss
from pso2boost.discord_events import ScheduledEventsApi
from pso2boost.fetch import Fetcher, SCHEDULE_URL
from pso2boost.maintenance import MaintenanceCalendar, fetch_calendar
from pso2boost.models import BoostEvent, merge_events
from pso2boost.notify import DiscordNotifier
from pso2boost.schedule import DEFAULT_KEYWORDS, filter_relevant, parse_schedule_html
from pso2boost.service import PlannedAction, enrich_event, plan_actions
from pso2boost.state import State
from pso2boost.sync import sync_events
from pso2boost.tz import now_jst

from .config import CloudConfig
from .discord import DiscordRest, DiscordRestError
from .storage import GuildConfig, SHARED_PARTITION, TableStore

log = logging.getLogger(__name__)


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
) -> list[BoostEvent]:
    """RSS から新着記事を拾い、スケジュールと合わせたイベント一覧を作る。

    RSS の既読は全サーバー共有（_shared パーティション）なので、
    pso2.jp への取得はサーバー数によらず1回で済む。
    """
    rss_events = discover_from_rss(fetcher, shared_state, now, calendar=calendar)
    return filter_relevant(merge_events(schedule_events, rss_events), now)


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
        result.last_error = "Discord イベントの同期に失敗"

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
            status = getattr(notifier, "last_status", None)
            detail = f" (HTTP {status})" if status else ""
            result.last_error = f"{action.kind} 通知の送信に失敗{detail}"
            continue
        result.notified += 1
        # 投稿に成功したものだけフラグを立てる（失敗は次回再試行）
        if action.kind == "new":
            work.state.mark_seen(action.event)
        work.state.mark_sent(action.kind, action.event.key)

    work.state.prune(days=config.state_retention_days)
    store.save_state(target.guild_id, work.state)
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
        guild for guild in store.list_guilds() if guild.enabled
    ]
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
) -> RunSummary:
    """Timer から呼ばれる週次バッチの入口。"""
    now = now or now_jst()
    summary = RunSummary()

    if not config.bot_token:
        summary.actions.append("DISCORD_BOT_TOKEN が未設定のため何もしません")
        log.error("DISCORD_BOT_TOKEN が未設定です")
        return summary

    store = store or TableStore(config.storage_connection)
    store.ensure_tables()
    store.prune_sessions(now)
    targets = resolve_targets(config, store)
    # Bot がキックされたサーバーは無効化して対象から外す（Phase 3）
    targets = filter_available_targets(
        targets, config, store, now, guild_client=guild_client
    )
    summary.guilds = len(targets)
    if not targets:
        summary.actions.append("対象サーバーがありません（/setup または環境変数で登録）")
        log.warning("対象サーバーがありません")
        return summary

    fetcher = fetcher or Fetcher(delay=config.delay)
    calendar, schedule_events = fetch_schedule(fetcher, now, config.keywords)

    # RSS の既読は全サーバー共有。ここで pso2.jp への取得は完了する
    shared_state = store.load_state(SHARED_PARTITION, now)
    events = discover_events(fetcher, shared_state, now, schedule_events, calendar)
    store.save_state(SHARED_PARTITION, shared_state)
    log.info("イベント解析: %d 件", len(events))

    # 準備は直列（PSO2 への HTTP を1回に抑える）
    works = [
        prepare_guild(target, events=events, now=now, store=store)
        for target in targets
    ]
    # 記事本文の取得もメインスレッドで1回だけ（イベントは全サーバー共通のオブジェクト）
    enriched: set[str] = set()
    for work in works:
        for action in work.actions:
            if action.event.key not in enriched:
                enrich_event(action.event, fetcher, now)
                enriched.add(action.event.key)

    def run_one(work: GuildWork) -> GuildResult:
        return process_guild(
            work,
            config=config,
            now=now,
            store=store,
            api=api_factory(work.target) if api_factory else None,
            notifier=notifier_factory(work.target) if notifier_factory else None,
        )

    workers = max(1, min(config.concurrency, len(works)))
    if workers == 1:
        results = [run_one(work) for work in works]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(run_one, works))

    for target, result in zip(targets, results):
        store.record_guild_run(
            target.guild_id, now, last_error=None if result.ok else result.last_error
        )
        if result.ok:
            log.info(
                "サーバー %s: 通知 %d / 作成 %d / 更新 %d / 削除 %d",
                target.guild_id,
                result.notified,
                result.created,
                result.updated,
                result.deleted,
            )
        else:
            summary.failed += 1
            log.error(
                "サーバー %s で失敗: %s（他のサーバーは続行）",
                target.guild_id,
                result.last_error,
            )

    summary.notified = sum(result.notified for result in results)
    summary.created = sum(result.created for result in results)
    summary.updated = sum(result.updated for result in results)
    summary.deleted = sum(result.deleted for result in results)
    summary.details = results
    log.info("週次同期完了: %s", summary.summary)
    return summary
