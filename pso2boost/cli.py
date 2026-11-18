"""コマンドラインインターフェース。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from urllib.parse import urlparse

from .article import apply_details, parse_article_html
from .discover import discover_from_rss
from .discord_events import ScheduledEventsApi
from .fetch import SITE, SCHEDULE_URL, FetchError, Fetcher
from .maintenance import fetch_calendar
from .models import BoostEvent, merge_events
from .notify import DiscordNotifier
from .periods import parse_period
from .schedule import DEFAULT_KEYWORDS, filter_relevant, parse_schedule_html
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


def _due_today(moment: datetime | None, now: datetime) -> bool:
    """その日の 9 時以降なら「本日」のお知らせを出してよい。"""
    if moment is None:
        return False
    return moment.astimezone(JST).date() == now.date() and now.hour >= 9


def _notifier(args) -> DiscordNotifier:
    return DiscordNotifier(
        webhook_url=os.getenv("DISCORD_WEBHOOK_URL"),
        mention_role_id=os.getenv("DISCORD_MENTION_ROLE_ID"),
        dry_run=args.dry_run,
        style=os.getenv("NOTIFY_STYLE", "discal"),
        bot_token=os.getenv("DISCORD_BOT_TOKEN"),
        channel_id=os.getenv("DISCORD_CHANNEL_ID"),
    )


def _discord_api() -> ScheduledEventsApi | None:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
    if not token or not guild_id:
        return None
    return ScheduledEventsApi(token, guild_id)


def _enrich(event: BoostEvent, fetcher: Fetcher, now: datetime) -> BoostEvent:
    """スケジュールの行に、告知記事の詳細（対象クエスト・倍率）を足す。"""
    if not event.url:
        return event
    base = event.url.split("#")[0]
    if urlparse(base).netloc and urlparse(base).netloc != urlparse(SITE).netloc:
        return event  # 外部サイト（bit.ly など）は本文を持っていない
    try:
        html = fetcher.get(base)
    except FetchError as exc:
        log.warning("告知記事を取得できませんでした（概要のみ通知）: %s", exc)
        return event
    article = parse_article_html(html, base, now)
    return apply_details(event, article)


def _was_pending_today(state: State, key: str, now: datetime) -> bool:
    first_seen = state.first_seen(key)
    if first_seen is None:
        return False
    return first_seen.astimezone(JST).date() == now.date()


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
    if not schedule_ok and not rss_events:
        return 2

    events = filter_relevant(merge_events(schedule_events, rss_events), now)
    log.info("%d 件のブースト関連イベントを確認しました", len(events))

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

    for event in events:
        enriched = False
        announced = False

        if not state.is_seen(event.key):
            _enrich(event, fetcher, now)
            enriched = True
            log.info("新着: %s (%s)", event.label, event.period.display())
            notifier.send_event(event, "new", event_link(event), now=now)
            new_count += 1
            announced = True
            if not effective_dry_run:
                state.mark_seen(event)
                state.mark_sent("new", event.key)

        # 告知したその日に始まる場合は、開始リマインドを重ねない
        if (
            not announced
            and _due_today(event.period.start.at, now)
            and not state.was_sent("start", event.key)
            and not _was_pending_today(state, event.key, now)
        ):
            if not enriched:
                _enrich(event, fetcher, now)
                enriched = True
            log.info("開始リマインド: %s", event.label)
            notifier.send_event(event, "start", event_link(event), now=now)
            reminder_count += 1
            if not effective_dry_run:
                state.mark_sent("start", event.key)

        if _due_today(event.period.end.at, now) and not state.was_sent("end", event.key):
            if not enriched:
                _enrich(event, fetcher, now)
            log.info("終了リマインド: %s", event.label)
            notifier.send_event(event, "end", event_link(event), now=now)
            reminder_count += 1
            if not effective_dry_run:
                state.mark_sent("end", event.key)

    if not effective_dry_run:
        state.prune()
        state.save()

    log.info(
        "完了: 新着 %d 件 / リマインド %d 件 / 状態 %s",
        new_count,
        reminder_count,
        "(dry-run のため未保存)" if effective_dry_run else f"{len(state)} 件",
    )
    return 0


def cmd_list(args) -> int:
    now = _parse_now(args.now)
    fetcher = Fetcher(delay=args.delay)
    try:
        html = fetcher.get(SCHEDULE_URL)
    except FetchError as exc:
        log.error("イベントスケジュールを取得できませんでした: %s", exc)
        return 2

    calendar = fetch_calendar(fetcher, now)
    events = filter_relevant(
        parse_schedule_html(html, now, _keywords(), calendar=calendar), now
    )
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
