from datetime import datetime, timedelta

from cloud.snapshot import decode_events, encode_events
from cloud.storage import SHARED_PARTITION, TableStore
from fakes import FakeTableService
from pso2boost.models import BoostEvent
from pso2boost.state import State
from pso2boost.periods import Period, Point
from pso2boost.tz import JST

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=JST)


def make_event() -> BoostEvent:
    return BoostEvent(
        key="abc123",
        label="PSO2の日（1～3日）",
        period=Period(
            start=Point(at=datetime(2026, 11, 1, 0, 0, tzinfo=JST), has_time=True),
            end=Point(at=datetime(2026, 11, 3, 23, 59, tzinfo=JST), has_time=True),
            raw="毎月1日 0:00~毎月3日 23:59",
        ),
        url="https://pso2.jp/players/news/31542/",
        description="終了時刻の明記がないため1時間",
        buffs_all=["レアドロップ倍率+100%"],
        buffs_premium=["レアドロップ倍率+50%"],
        source="pso2day",
    )


def test_round_trip_preserves_event_fields():
    original = make_event()

    restored = decode_events(encode_events([original]))[0]

    assert restored == original


def test_storage_snapshot_expires_by_ttl():
    store = TableStore("unused", service=FakeTableService())
    store.ensure_tables()
    store.save_event_snapshot(encode_events([make_event()]), NOW)

    assert store.load_event_snapshot(NOW + timedelta(minutes=5), 600) is not None
    assert store.load_event_snapshot(NOW + timedelta(minutes=11), 600) is None
    assert store.load_event_snapshot(NOW, 0) is None


def test_saving_shared_state_keeps_snapshot(tmp_path):
    store = TableStore("unused", service=FakeTableService())
    store.ensure_tables()
    store.save_event_snapshot(encode_events([make_event()]), NOW)

    store.save_state(SHARED_PARTITION, State(tmp_path / "state.json", NOW))

    assert store.load_event_snapshot(NOW, 600) is not None


def test_missing_snapshot_returns_none():
    store = TableStore("unused", service=FakeTableService())
    store.ensure_tables()

    assert store.load_event_snapshot(NOW, 600) is None
