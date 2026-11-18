from datetime import datetime
from pathlib import Path

from pso2boost.article import apply_details, events_from_article, parse_article_html
from pso2boost.models import BoostEvent
from pso2boost.periods import parse_period
from pso2boost.tz import JST

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://pso2.jp/players/news/32486/"
ENHANCE_URL = "https://pso2.jp/players/news/32453/"


def load(name: str = "article_32486.html") -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def parse(now=None):
    now = now or datetime(2026, 11, 18, 12, 0, tzinfo=JST)
    return parse_article_html(load(), URL, now)


def test_finds_boost_block_and_rows():
    article = parse()
    assert article.blocks, "ブーストブロックが見つからない"
    block = article.blocks[0]
    assert "ブースト" in block.heading
    assert len(block.rows) == 2

    first = block.rows[0]
    assert first.label == "ブーストキャンペーン 第1弾"
    assert first.quests == ["アークス双陣杯", "星砕の暴進"]
    assert first.buffs_all == ["レアドロップ倍率+100%", "プリセット能力付与倍率+50%"]
    assert first.buffs_premium == ["レアドロップ倍率+50%", "プリセット能力付与倍率+50%"]
    assert first.period.start.at == datetime(2026, 11, 11, 12, 0, tzinfo=JST)


def test_intro_comes_from_block_not_period():
    block = parse().blocks[0]
    assert "ブーストキャンペーンを開催" in block.intro
    assert "12:00" not in block.intro


def test_apply_details_matches_label():
    event = BoostEvent(
        key="x",
        label="ブーストキャンペーン 第2弾",
        period=parse_period("11/25 12:00～12/2 メンテナンス開始まで", 2026),
        url=URL,
    )
    apply_details(event, parse())
    assert event.quests == ["リレーションクエスト", "星砕の暴進"]
    assert event.article_title.startswith("コスチュームGETキャンペーン")
    assert event.description


def test_parses_enhancement_campaign():
    now = datetime(2026, 10, 8, 12, 0, tzinfo=JST)
    article = parse_article_html(load("article_32453.html"), ENHANCE_URL, now)

    # 2列のトレジャーショップ表は無視され、強化応援だけが残る
    assert len(article.blocks) == 1
    block = article.blocks[0]
    assert block.heading == "強化応援キャンペーン"
    assert "強化応援キャンペーンを開催" in block.intro

    assert len(block.rows) == 1
    row = block.rows[0]
    assert row.label == "強化応援キャンペーン"
    assert len(row.quests) == 8
    assert "強化大成功率+100%" in row.buffs_all
    assert row.buffs_premium == []
    assert row.period.start.at == datetime(2026, 10, 7, 11, 0, tzinfo=JST)
    assert row.period.end.at == datetime(2026, 11, 4, 11, 0, tzinfo=JST)


def test_enhancement_campaign_becomes_event():
    now = datetime(2026, 10, 8, 12, 0, tzinfo=JST)
    article = parse_article_html(load("article_32453.html"), ENHANCE_URL, now)
    events = events_from_article(article)

    assert len(events) == 1
    event = events[0]
    assert event.label == "強化応援キャンペーン"
    assert event.buffs_all
    assert event.location.endswith("ほか5件")
