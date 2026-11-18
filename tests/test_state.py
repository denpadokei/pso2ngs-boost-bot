from datetime import datetime
from pathlib import Path

from pso2boost.models import BoostEvent, event_key
from pso2boost.periods import parse_period
from pso2boost.state import State
from pso2boost.tz import JST

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=JST)


def make_event(label: str = "ブーストキャンペーン 第1弾") -> BoostEvent:
    period = parse_period("11/11 12:00～11/18 メンテナンス開始まで", 2026)
    return BoostEvent(key=event_key(label, period), label=label, period=period)


def test_roundtrip(tmp_path: Path):
    path = tmp_path / "state.json"
    state = State(path, NOW)
    event = make_event()
    state.mark_seen(event)
    state.mark_sent("new", event.key)
    state.save()

    reloaded = State(path, NOW)
    assert reloaded.is_seen(event.key)
    assert reloaded.was_sent("new", event.key)
    assert not reloaded.was_sent("start", event.key)
    assert reloaded.first_seen(event.key) == NOW


def test_period_change_creates_new_key():
    first = make_event()
    changed = BoostEvent(
        key=event_key("ブーストキャンペーン 第1弾", parse_period("11/11 12:00～11/19 メンテナンス開始まで", 2026)),
        label="ブーストキャンペーン 第1弾",
        period=parse_period("11/11 12:00～11/19 メンテナンス開始まで", 2026),
    )
    assert first.key != changed.key


def test_prune_removes_old_entries(tmp_path: Path):
    path = tmp_path / "state.json"
    old = datetime(2025, 1, 1, 0, 0, tzinfo=JST)
    state = State(path, old)
    state.mark_seen(make_event("古いイベント"))
    state.save()

    later = State(path, NOW)
    later.prune(days=30)
    assert len(later) == 0


def test_broken_file_is_ignored(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("{ this is not json", encoding="utf-8")
    state = State(path, NOW)
    assert len(state) == 0
