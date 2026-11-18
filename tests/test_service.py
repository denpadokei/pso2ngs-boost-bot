from datetime import datetime

from pso2boost.models import BoostEvent, event_key
from pso2boost.periods import parse_period
from pso2boost.service import plan_actions
from pso2boost.state import State
from pso2boost.tz import JST

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=JST)


def make_event(label: str, period_text: str) -> BoostEvent:
    period = parse_period(period_text, 2026)
    return BoostEvent(key=event_key(label, period), label=label, period=period)


def kinds(actions) -> list[str]:
    return [action.kind for action in actions]


def test_unseen_event_plans_new(tmp_path):
    event = make_event("ブーストキャンペーン 第2弾", "11/25 12:00～12/2 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)

    actions = plan_actions([event], state, NOW)

    assert kinds(actions) == ["new"]
    assert actions[0].event is event


def test_seen_event_plans_nothing(tmp_path):
    event = make_event("ブーストキャンペーン 第2弾", "11/25 12:00～12/2 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)
    state.mark_seen(event)

    assert plan_actions([event], state, NOW) == []


def test_start_due_today_plans_start(tmp_path):
    event = make_event("ブーストキャンペーン 第1弾", "11/18 12:00～11/25 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)
    state.mark_seen(event)  # first_seen は昨日にしておく
    state._data["seen"][event.key]["first_seen"] = "2026-11-17T12:00:00+09:00"

    assert kinds(plan_actions([event], state, NOW)) == ["start"]


def test_start_not_planned_when_announced_today(tmp_path):
    """当日に新着告知したものは、開始リマインドを重ねない。"""
    event = make_event("ブーストキャンペーン 第1弾", "11/18 12:00～11/25 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)
    # 未seen なので new が計画され、start は重ならない
    assert kinds(plan_actions([event], state, NOW)) == ["new"]


def test_start_not_planned_when_already_pending_today(tmp_path):
    """当日初めて見た（＝初回実行で告知済み）イベントは start を出さない。"""
    event = make_event("ブーストキャンペーン 第1弾", "11/18 12:00～11/25 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)
    state.mark_seen(event)  # first_seen == now（当日）
    state.mark_sent("new", event.key)

    assert plan_actions([event], state, NOW) == []


def test_start_not_planned_before_nine(tmp_path):
    now = datetime(2026, 11, 18, 8, 0, tzinfo=JST)
    event = make_event("ブーストキャンペーン 第1弾", "11/18 12:00～11/25 メンテナンス開始まで")
    state = State(tmp_path / "state.json", now)
    state.mark_seen(event)
    state._data["seen"][event.key]["first_seen"] = "2026-11-17T12:00:00+09:00"

    assert plan_actions([event], state, now) == []


def test_end_due_today_plans_end(tmp_path):
    event = make_event("ブーストキャンペーン 第1弾", "11/11 12:00～11/18 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)
    state.mark_seen(event)
    state.mark_sent("new", event.key)
    state.mark_sent("start", event.key)

    assert kinds(plan_actions([event], state, NOW)) == ["end"]


def test_end_not_planned_twice(tmp_path):
    event = make_event("ブーストキャンペーン 第1弾", "11/11 12:00～11/18 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)
    state.mark_seen(event)
    state.mark_sent("new", event.key)
    state.mark_sent("start", event.key)
    state.mark_sent("end", event.key)

    assert plan_actions([event], state, NOW) == []


def test_new_and_end_can_coexist(tmp_path):
    """告知と終了が同じ日なら、両方の通知を計画する。"""
    event = make_event("ブーストキャンペーン 第0弾", "11/11 12:00～11/18 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)

    assert kinds(plan_actions([event], state, NOW)) == ["new", "end"]


def test_plan_does_not_mutate_state(tmp_path):
    event = make_event("ブーストキャンペーン 第2弾", "11/25 12:00～12/2 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)

    plan_actions([event], state, NOW)

    assert not state.is_seen(event.key)
    assert not state.was_sent("new", event.key)


def test_multiple_events_keep_order(tmp_path):
    first = make_event("ブーストキャンペーン 第1弾", "11/11 12:00～11/18 メンテナンス開始まで")
    second = make_event("ブーストキャンペーン 第2弾", "11/25 12:00～12/2 メンテナンス開始まで")
    state = State(tmp_path / "state.json", NOW)

    actions = plan_actions([first, second], state, NOW)

    assert kinds(actions) == ["new", "end", "new"]
    assert actions[0].event is first
    assert actions[2].event is second
