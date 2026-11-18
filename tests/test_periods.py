from datetime import datetime

import pytest

from pso2boost.periods import normalize, parse_period
from pso2boost.tz import JST


def jst(*args) -> datetime:
    return datetime(*args, tzinfo=JST)


def test_schedule_period_with_maintenance_end():
    period = parse_period("11/11 12:00～11/18 メンテナンス開始まで", 2026)
    assert period.start.at == jst(2026, 11, 11, 12, 0)
    assert period.start.has_time is True
    assert period.end.at == jst(2026, 11, 18, 11, 0)
    assert period.end.has_time is False
    assert period.end.note == "メンテナンス開始"


def test_article_period_with_weekday_marks():
    period = parse_period(
        "2026/11/11（水）12:00 ～2026/11/18（水）メンテナンス開始", 2026
    )
    assert period.start.at == jst(2026, 11, 11, 12, 0)
    assert period.end.at == jst(2026, 11, 18, 11, 0)


def test_undecided_end():
    period = parse_period("2023/10/11 定期メンテナンス終了～未定", 2026)
    assert period.start.at == jst(2023, 10, 11, 11, 0)
    assert period.end.at is None
    assert "未定" in period.display()


def test_year_rollover():
    period = parse_period("12/25 12:00～1/10 メンテナンス開始まで", 2026)
    assert period.start.at == jst(2026, 12, 25, 12, 0)
    assert period.end.at == jst(2027, 1, 10, 11, 0)


def test_single_day_period():
    period = parse_period("2026/11/21 00:00～2026/11/23 23:59", 2026)
    assert period.start.at == jst(2026, 11, 21, 0, 0)
    assert period.end.at == jst(2026, 11, 23, 23, 59)


def test_display_shape():
    period = parse_period("11/11 12:00～11/18 メンテナンス開始まで", 2026)
    assert period.display() == "2026/11/11 12:00 ～ 2026/11/18 メンテナンス開始"


def test_normalize_fullwidth_digits():
    assert normalize("１１／１１　１２：００") == "11/11 12:00"


@pytest.mark.parametrize("text", ["", "未定", "～"])
def test_unparsable_period_does_not_crash(text):
    period = parse_period(text, 2026)
    assert period.start.at is None or period.start.at.year == 2026
