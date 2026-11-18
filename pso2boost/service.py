"""CLI とクラウドで共有する「何をいつ通知するか」の判定。

このモジュールは状態の保存も通知の送信も行わない。
``plan_actions`` が返す計画を呼び出し側がどう実行するか（保存するか、
dry-run か、失敗時にフラグを立てるか）は、CLI とクラウドランナーで異なる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import BoostEvent
from .state import State
from .tz import JST


def due_today(moment: datetime | None, now: datetime) -> bool:
    """その日の 9 時以降なら「本日」のお知らせを出してよい。"""
    if moment is None:
        return False
    return moment.astimezone(JST).date() == now.date() and now.hour >= 9


def _was_pending_today(state: State, key: str, now: datetime) -> bool:
    first_seen = state.first_seen(key)
    if first_seen is None:
        return False
    return first_seen.astimezone(JST).date() == now.date()


@dataclass(frozen=True)
class PlannedAction:
    """1回の通知送信。``kind`` は "new" / "start" / "end"。"""

    event: BoostEvent
    kind: str


def plan_actions(
    events: list[BoostEvent], state: State, now: datetime
) -> list[PlannedAction]:
    """現在の状態から、送るべき通知の一覧を決める。

    - 新着: まだ見たことがないイベント
    - 開始リマインド: 開始日当日（9時以降）で未送信かつ、当日に新着告知していないもの
    - 終了リマインド: 終了日当日（9時以降）で未送信のもの

    告知（new）と開始が同じ日なら、開始リマインドは重ねない。
    """
    actions: list[PlannedAction] = []
    for event in events:
        announced = False

        if not state.is_seen(event.key):
            actions.append(PlannedAction(event, "new"))
            announced = True

        # 告知したその日に始まる場合は、開始リマインドを重ねない
        if (
            not announced
            and due_today(event.period.start.at, now)
            and not state.was_sent("start", event.key)
            and not _was_pending_today(state, event.key, now)
        ):
            actions.append(PlannedAction(event, "start"))

        if due_today(event.period.end.at, now) and not state.was_sent("end", event.key):
            actions.append(PlannedAction(event, "end"))

    return actions
