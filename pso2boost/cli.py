"""コマンドラインインターフェース。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime

from .article import parse_article_html
from .discover import discover_from_rss
from .discord_events import ScheduledEventsApi
from .fetch import SCHEDULE_URL, FetchError, Fetcher
from .lookup import TEXT_CHANNEL_TYPES, DiscordLookup, DiscordLookupError
from .maintenance import fetch_calendar
from .models import BoostEvent, merge_events
from .notify import DiscordNotifier
from .periods import parse_period
from .pso2day import discover_pso2_day
from .schedule import DEFAULT_KEYWORDS, filter_relevant, parse_schedule_html
from .service import enrich_event, plan_actions
from .state import State
from .sync import sync_events
from .tz import JST, now_jst

log = logging.getLogger("pso2boost")


def _keywords() -> tuple[str, ...]:
    raw = os.getenv("BOOST_KEYWORDS", "").strip()
    if not raw:
        return DEFAULT_KEYWORDS
    return tuple(word.strip() for word in raw.split(",") if word.strip())


def _parse_now(value: str | None) -> datetime:
    if not value:
        return now_jst()
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=JST) if parsed.tzinfo is None else parsed.astimezone(JST)


def _notifier(args) -> DiscordNotifier:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    channel_id = "" if os.getenv("DISCORD_WEBHOOK_URL") else _resolve_channel_id(token)
    return DiscordNotifier(
        webhook_url=os.getenv("DISCORD_WEBHOOK_URL"),
        mention_role_id=os.getenv("DISCORD_MENTION_ROLE_ID"),
        dry_run=args.dry_run,
        style=os.getenv("NOTIFY_STYLE", "discal"),
        bot_token=token,
        channel_id=channel_id,
    )


def _discord_api() -> ScheduledEventsApi | None:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return None
    guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
    try:
        guild_id = DiscordLookup(token).resolve_guild_id(guild_id or None)
    except DiscordLookupError as exc:
        log.error("サーバーを特定できませんでした: %s", exc)
        return None
    return ScheduledEventsApi(token, guild_id)


def _resolve_channel_id(token: str) -> str:
    channel_id = os.getenv("DISCORD_CHANNEL_ID", "").strip()
    if channel_id:
        return channel_id

    name = os.getenv("DISCORD_CHANNEL_NAME", "").strip()
    if not name or not token:
        return ""

    try:
        lookup = DiscordLookup(token)
        guild_id = lookup.resolve_guild_id(os.getenv("DISCORD_GUILD_ID", "").strip() or None)
        return lookup.resolve_channel_id(guild_id, name=name)
    except DiscordLookupError as exc:
        log.error("投稿先チャンネルを特定できませんでした: %s", exc)
        return ""


def _enrich(event: BoostEvent, fetcher: Fetcher, now: datetime) -> BoostEvent:
    """（service.enrich_event のエイリアス。CLI 内の呼び出しをそのまま残す。）"""
    return enrich_event(event, fetcher, now)


def cmd_check(args) -> int:
    now = _parse_now(args.now)
    keywords = _keywords()
    notifier = _notifier(args)
    effective_dry_run = args.dry_run or not notifier.enabled
    if not notifier.enabled and not args.dry_run:
        log.warning("DISCORD_WEBHOOK_URL が未設定のため、通知内容を表示するだけにします。")

    state = State(args.state, now)
    fetcher = Fetcher(delay=args.delay)

    schedule_events: list[BoostEvent] = []
    schedule_ok = True
    calendar = fetch_calendar(fetcher, now)
    if len(calendar):
        log.info("メンテナンス予定: %s", calendar.describe())
    try:
        schedule_events = parse_schedule_html(
            fetcher.get(SCHEDULE_URL), now, keywords, calendar=calendar
        )
    except FetchError as exc:
        schedule_ok = False
        log.error("イベントスケジュールを取得できませんでした: %s", exc)

    rss_events = discover_from_rss(fetcher, state, now, calendar=calendar)
    pso2day = discover_pso2_day(fetcher, now)
    if not schedule_ok and not rss_events and not pso2day.ok:
        return 2

    events = filter_relevant(
        merge_events(merge_events(schedule_events, rss_events), pso2day.events), now
    )
    log.info("%d 件のブースト関連イベントを確認しました", len(events))

    actions = plan_actions(events, state, now)
    # 共有 Bot と同じく、同期前に記事の詳細を揃える。
    # 開始済みイベントは contents だけ更新するため、詳細が無いと古い場所のままになる。
    enriched_keys: set[str] = set()
    for action in actions:
        if action.event.key not in enriched_keys:
            _enrich(action.event, fetcher, now)
            enriched_keys.add(action.event.key)

    api = _discord_api()
    sync_result = None
    if api is not None:
        sync_result = sync_events(events, state, api, now, dry_run=effective_dry_run)
        log.info("Discord イベント同期: %s", sync_result.summary)
    elif not args.dry_run:
        log.info(
            "DISCORD_BOT_TOKEN / DISCORD_GUILD_ID が未設定のため、"
            "Discord イベントの作成は行いません。"
        )

    def event_link(event: BoostEvent) -> str | None:
        return sync_result.links.get(event.key) if sync_result else None

    new_count = 0
    reminder_count = 0
    failed_count = 0

    for action in actions:
        event = action.event

        if action.kind == "new":
            log.info("新着: %s (%s)", event.label, event.period.display())
        else:
            log.info(
                "%s: %s",
                "開始リマインド" if action.kind == "start" else "終了リマインド",
                event.label,
            )
        sent = notifier.send_event(event, action.kind, event_link(event), now=now)
        if action.kind == "new":
            new_count += 1
        else:
            reminder_count += 1

        if effective_dry_run:
            continue
        if not sent:
            # 投稿に失敗したらフラグを立てず、次回の実行で再試行する
            log.error("通知に失敗したため状態を進めません: %s (%s)", event.label, action.kind)
            failed_count += 1
            continue
        if action.kind == "new":
            state.mark_seen(event)
            state.mark_sent("new", event.key)
        else:
            state.mark_sent(action.kind, event.key)

    if not effective_dry_run:
        state.prune()
        state.save()

    log.info(
        "完了: 新着 %d 件 / リマインド %d 件 / 状態 %s",
        new_count,
        reminder_count,
        "(dry-run のため未保存)" if effective_dry_run else f"{len(state)} 件",
    )
    return 2 if failed_count else 0


def cmd_list(args) -> int:
    now = _parse_now(args.now)
    fetcher = Fetcher(delay=args.delay)
    try:
        html = fetcher.get(SCHEDULE_URL)
    except FetchError as exc:
        log.error("イベントスケジュールを取得できませんでした: %s", exc)
        return 2

    calendar = fetch_calendar(fetcher, now)
    schedule_events = parse_schedule_html(html, now, _keywords(), calendar=calendar)
    pso2day = discover_pso2_day(fetcher, now)
    if not pso2day.ok:
        log.warning("PSO2の日の情報が既定値です（取得または解析に失敗）")
    events = filter_relevant(merge_events(schedule_events, pso2day.events), now)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "label": event.label,
                        "period": event.period.display(),
                        "start": event.period.start.at.isoformat()
                        if event.period.start.at
                        else None,
                        "end": event.period.end.at.isoformat() if event.period.end.at else None,
                        "url": event.url,
                    }
                    for event in events
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not events:
        print("現在、ブースト関連の予定はありません。")
        return 0

    for event in events:
        print(f"・{event.label}")
        print(f"  {event.period.display()}")
        if event.url:
            print(f"  {event.url}")
    return 0


def cmd_channels(args) -> int:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        log.error("DISCORD_BOT_TOKEN が未設定です")
        return 2

    lookup = DiscordLookup(token)
    try:
        guilds = lookup.guilds()
    except DiscordLookupError as exc:
        log.error("%s", exc)
        return 2

    if not guilds:
        print("Bot はどのサーバーにも参加していません。先に招待してください。")
        return 1

    for guild in guilds:
        print(f"{guild.get('name')}  DISCORD_GUILD_ID={guild.get('id')}")
        try:
            channels = lookup.channels(str(guild["id"]))
        except DiscordLookupError as exc:
            print(f"  チャンネルを取得できませんでした: {exc}")
            continue
        for channel in channels:
            if channel.get("type") in TEXT_CHANNEL_TYPES:
                print(f"  #{channel.get('name')}  DISCORD_CHANNEL_ID={channel.get('id')}")
    return 0


def cmd_preview(args) -> int:
    now = _parse_now(args.now)
    fetcher = Fetcher(delay=args.delay)
    try:
        html = fetcher.get(args.url)
    except FetchError as exc:
        log.error("記事を取得できませんでした: %s", exc)
        return 2

    article = parse_article_html(html, args.url, now, fetch_calendar(fetcher, now))
    print(f"タイトル: {article.title}")
    if not article.blocks:
        print("ブーストキャンペーンの表は見つかりませんでした。")
        return 1

    for block in article.blocks:
        print(f"\n[{block.heading}]")
        if block.period:
            print(f"期間: {block.period.display()}")
        if block.intro:
            print(f"説明: {block.intro}")
        for row in block.rows:
            print(f"\n  ■ {row.label}")
            print(f"    期間: {row.period.display()}")
            if row.quests:
                print(f"    対象: {', '.join(row.quests)}")
            if row.buffs_all:
                print(f"    全員: {', '.join(row.buffs_all)}")
            if row.buffs_premium:
                print(f"    プレミアム: {', '.join(row.buffs_premium)}")
    return 0


def cmd_test_notify(args) -> int:
    now = _parse_now(args.now)
    notifier = _notifier(args)
    fetcher = Fetcher(delay=args.delay)

    event: BoostEvent | None = None
    if args.url:
        try:
            article = parse_article_html(
                fetcher.get(args.url), args.url, now, fetch_calendar(fetcher, now)
            )
        except FetchError as exc:
            log.error("記事を取得できませんでした: %s", exc)
            return 2
        if article.blocks and article.blocks[0].rows:
            block = article.blocks[0]
            row = block.rows[0]
            event = BoostEvent(
                key="test",
                label=row.label,
                period=row.period,
                url=args.url,
                article_title=article.title,
                description=block.intro,
                quests=row.quests,
                buffs_all=row.buffs_all,
                buffs_premium=row.buffs_premium,
            )

    if event is None:
        try:
            events = filter_relevant(
                parse_schedule_html(
                    fetcher.get(SCHEDULE_URL),
                    now,
                    _keywords(),
                    calendar=fetch_calendar(fetcher, now),
                ),
                now,
            )
        except FetchError as exc:
            log.error("イベントスケジュールを取得できませんでした: %s", exc)
            return 2
        if events:
            event = events[0]
        else:
            event = BoostEvent(
                key="test",
                label="ブーストキャンペーン（テスト）",
                period=parse_period("11/11 12:00～11/18 メンテナンス開始まで", now.year),
                url=SCHEDULE_URL,
            )

    sent = notifier.send_event(event, "new", now=now)
    if sent:
        print("テスト通知を送信しました。")
        return 0
    log.error("テスト通知を送信できませんでした（DISCORD_WEBHOOK_URL / Bot の権限を確認）")
    return 1


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(
        prog="pso2boost",
        description="PSO2NGS のブーストキャンペーンを Discord に通知する",
    )
    parser.add_argument("--verbose", action="store_true", help="デバッグログを出す")

    sub = parser.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--dry-run", action="store_true", help="投稿せず内容を表示する")
        sp.add_argument("--now", help="基準日時を上書き（例: 2026-11-11T12:00）")
        sp.add_argument("--delay", type=float, default=1.0, help="リクエスト間隔（秒）")

    check = sub.add_parser("check", help="新着・開始・終了を確認して通知する")
    common(check)
    check.add_argument(
        "--state",
        default=os.getenv("STATE_PATH", "data/state.json"),
        help="状態ファイルのパス",
    )
    check.set_defaults(func=cmd_check)

    list_cmd = sub.add_parser("list", help="現在のブースト予定を表示する")
    common(list_cmd)
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=cmd_list)

    channels = sub.add_parser(
        "channels", help="Bot が参加しているサーバーとチャンネルIDを表示する"
    )
    channels.set_defaults(func=cmd_channels)

    preview = sub.add_parser("preview", help="告知記事の解析結果を表示する")
    preview.add_argument("url")
    common(preview)
    preview.set_defaults(func=cmd_preview)

    test = sub.add_parser("test-notify", help="Webhook の疎通確認")
    common(test)
    test.add_argument("--url", help="告知記事の URL（省略時は現在の予定を使用）")
    test.set_defaults(func=cmd_test_notify)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        return args.func(args)
    except KeyboardInterrupt:  # pragma: no cover
        return 130


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv は任意
        return
    load_dotenv()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
