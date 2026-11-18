"""Discord への通知（Webhook）。

見た目は DisCal のイベント告知に合わせた「イベントカード」形式。
Bot アカウント（Gateway 接続）は不要で、チャンネルの Webhook URL だけで投稿できる。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

import requests

from .models import BoostEvent
from .periods import Point
from .tz import now_jst

log = logging.getLogger(__name__)

COLOR_DISCAL = 0x5865F2
COLOR_COMPACT = 0x2F9BE0
FOOTER = "PSO2NGS ブースト通知（非公式）"

WEEKDAYS = ("月", "火", "水", "木", "金", "土", "日")

TITLES = {
    "new": "!~Event Announcement~!",
    "start": "!~Event Announcement~!",
    "end": "!~Event Announcement~!",
}

INTROS = {
    "new": "新しいキャンペーンが告知されました。",
    "start": "本日からキャンペーンが実施されます。",
    "end": "本日でキャンペーンが終了します。",
}

KIND_ICONS = {"new": "🆕", "start": "▶️", "end": "⏰"}


def _end_has_passed(event: BoostEvent, now: datetime) -> bool:
    end = event.period.end.at
    return end is not None and end <= now


def _kind_mark(event: BoostEvent, kind: str, now: datetime) -> str | None:
    if kind == "start":
        return "【本日から】"
    if kind == "end":
        return "【終了】" if _end_has_passed(event, now) else "【本日まで】"
    return None


def _intro(event: BoostEvent, kind: str, now: datetime) -> str:
    if kind == "end" and _end_has_passed(event, now):
        return "キャンペーンが終了しました。"
    return INTROS.get(kind, "")


def _field(name: str, value: str, inline: bool = False) -> dict:
    text = value.strip() or "-"
    if len(text) > 1024:
        text = text[:1020] + "…"
    return {"name": name[:256], "value": text, "inline": inline}


def _bullets(values: list[str]) -> str:
    return "\n".join(f"・{value}" for value in values)


def _format_local(point: Point) -> str:
    """DisCal 風の日時表記。時刻が未定なら注記をそのまま出す。"""
    if point.at is None:
        return point.note or "未定"
    date = (
        f"{point.at.year}年{point.at.month}月{point.at.day}日 "
        f"{WEEKDAYS[point.at.weekday()]}曜日"
    )
    if point.has_time:
        return f"{date} {point.at:%H:%M}"
    return f"{date} {point.note}".strip()


def _description(event: BoostEvent, kind: str, now: datetime) -> str:
    lines: list[str] = []
    mark = _kind_mark(event, kind, now)
    if mark:
        lines.append(mark)
    if event.buffs_all:
        lines.append(_bullets(event.buffs_all))
    elif event.description:
        lines.append(event.description)
    return "\n".join(line for line in lines if line)


def _location(event: BoostEvent) -> str:
    return event.location


def build_discal_embed(
    event: BoostEvent,
    kind: str,
    now: datetime | None = None,
    event_url: str | None = None,
) -> dict:
    moment = now or now_jst()
    fields = [
        _field("Name", event.label),
        _field("Description", _description(event, kind, moment)),
        _field("Start (Local)", _format_local(event.period.start), inline=True),
        _field("End (Local)", _format_local(event.period.end), inline=True),
        _field("Location", _location(event)),
    ]
    if event.buffs_premium:
        fields.append(_field("Premium", _bullets(event.buffs_premium)))
    fields.append(_field("Event ID", event.key, inline=True))
    if event_url:
        fields.append(_field("イベント", f"[RSVP / イベントページ]({event_url})"))
    if event.url:
        fields.append(_field("告知記事", f"[PSO2NGS 公式サイト]({event.url})"))

    return {
        "title": TITLES.get(kind, TITLES["new"]),
        "color": COLOR_DISCAL,
        "fields": fields,
        "footer": {"text": f"Announcement ID: {event.key}"},
        "timestamp": moment.isoformat(),
    }


def build_compact_embed(event: BoostEvent, kind: str, now: datetime | None = None) -> dict:
    moment = now or now_jst()
    description_lines = [_intro(event, kind, moment)]
    if event.description:
        description_lines.append(event.description)

    embed: dict = {
        "title": f"{KIND_ICONS.get(kind, '')} {event.label}".strip(),
        "description": "\n".join(line for line in description_lines if line),
        "color": COLOR_COMPACT,
        "fields": [_field("開催期間", event.period.display())],
        "footer": {"text": FOOTER},
        "timestamp": moment.isoformat(),
    }
    if event.url:
        embed["url"] = event.url
    if event.quests:
        embed["fields"].append(_field("対象クエスト", "\n".join(event.quests)))
    if event.buffs_all:
        embed["fields"].append(_field("すべてのプレイヤー", "\n".join(event.buffs_all)))
    if event.buffs_premium:
        embed["fields"].append(_field("プレミアムセット有効期間中", "\n".join(event.buffs_premium)))
    return embed


def build_embed(
    event: BoostEvent,
    kind: str,
    now: datetime | None = None,
    style: str = "discal",
    event_url: str | None = None,
) -> dict:
    if style == "compact":
        return build_compact_embed(event, kind, now)
    return build_discal_embed(event, kind, now, event_url=event_url)


def render_text(
    event: BoostEvent,
    kind: str,
    style: str = "discal",
    event_url: str | None = None,
    now: datetime | None = None,
) -> str:
    embed = build_embed(event, kind, now=now, style=style, event_url=event_url)
    lines = [f"[{kind}] {embed['title']}"]
    if embed.get("description"):
        lines.append(embed["description"])
    for field in embed["fields"]:
        lines.append(f"{field['name']}: {field['value']}")
    if embed.get("url"):
        lines.append(f"URL: {embed['url']}")
    return "\n".join(lines)


class DiscordNotifier:
    def __init__(
        self,
        webhook_url: str | None = None,
        mention_role_id: str | None = None,
        dry_run: bool = False,
        timeout: float = 20.0,
        style: str = "discal",
        username: str | None = None,
        avatar_url: str | None = None,
        bot_token: str | None = None,
        channel_id: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.webhook_url = (webhook_url or "").strip()
        self.mention_role_id = (mention_role_id or "").strip()
        self.dry_run = dry_run
        self.timeout = timeout
        self.style = style if style in {"discal", "compact"} else "discal"
        self.username = (username or os.getenv("DISCORD_WEBHOOK_USERNAME", "")).strip()
        self.avatar_url = (avatar_url or os.getenv("DISCORD_WEBHOOK_AVATAR_URL", "")).strip()
        self.bot_token = (bot_token or "").strip()
        self.channel_id = (channel_id or "").strip()
        self.session = session or requests

    @property
    def enabled(self) -> bool:
        if self.dry_run:
            return False
        return bool(self.webhook_url) or bool(self.bot_token and self.channel_id)

    def _endpoint(self) -> tuple[str, dict[str, str]]:
        if self.webhook_url:
            return self.webhook_url, {}
        return (
            f"https://discord.com/api/v10/channels/{self.channel_id}/messages",
            {"Authorization": f"Bot {self.bot_token}"},
        )

    def send_event(
        self,
        event: BoostEvent,
        kind: str,
        event_url: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        embed = build_embed(event, kind, now=now, style=self.style, event_url=event_url)
        content = None
        if kind == "new" and self.mention_role_id:
            content = f"<@&{self.mention_role_id}>"

        if not self.enabled:
            print(render_text(event, kind, style=self.style, event_url=event_url, now=now))
            print("-" * 60)
            return False

        return self._post(embed, content)

    def _post(self, embed: dict, content: str | None) -> bool:
        payload: dict = {"embeds": [embed], "allowed_mentions": {"parse": ["roles"]}}
        if content:
            payload["content"] = content
        if self.webhook_url:
            if self.username:
                payload["username"] = self.username
            if self.avatar_url:
                payload["avatar_url"] = self.avatar_url

        url, headers = self._endpoint()
        for attempt in (1, 2):
            try:
                response = self.session.post(
                    url, json=payload, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                log.error("Discord への接続に失敗: %s", exc)
                return False
            if response.status_code == 429 and attempt == 1:
                retry_after = 1.0
                try:
                    retry_after = float(response.json().get("retry_after", 1.0))
                except ValueError:
                    pass
                time.sleep(min(retry_after, 5.0))
                continue
            if response.status_code >= 400:
                log.error(
                    "Discord への投稿に失敗: HTTP %s %s",
                    response.status_code,
                    response.text[:300],
                )
                return False
            return True
        return False
