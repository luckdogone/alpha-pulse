import pytest
from pydantic import ValidationError

from application.demo import demo_snapshot
from domain.decision import Condition, DecisionTree
from domain.indicators import calculate, ema
from domain.models import Candle, PriceZone
from domain.quality import assess_quality
from storage.evidence import EvidenceStore


def test_ema_is_sma_seeded():
    assert ema([1, 2, 3, 4, 5], 3) == [2, 3, 4]


def test_forming_candle_does_not_affect_indicators(market_fixture):
    snapshot, _, _ = market_fixture
    expected = calculate(snapshot.candles)
    rows = list(snapshot.candles)
    rows[-1] = rows[-1].model_copy(update={"close": 100_000, "high": 100_001})
    assert calculate(rows) == expected


def test_flat_market_rsi_and_atr(market_fixture):
    snapshot, _, _ = market_fixture
    rows = [
        c.model_copy(update={"open": 10, "high": 10, "low": 10, "close": 10})
        for c in snapshot.candles
    ]
    result = calculate(rows)
    assert result["rsi14"] == 50
    assert result["atr14"] == 0
    assert result["macd_histogram"] == 0


@pytest.mark.parametrize(
    "bad",
    [
        {"high": 1},
        {"close": float("nan")},
        {"volume": -1},
        {"close_time": 1},
        {"taker_buy_volume": 100_000},
    ],
)
def test_candle_rejects_corrupt_values(market_fixture, bad):
    row = market_fixture[0].candles[0].model_dump() | bad
    with pytest.raises(ValidationError):
        Candle.model_validate(row)


def test_stale_book_blocks_direction(market_fixture):
    snapshot, store, tree = market_fixture
    record = store.records[snapshot.required_ids[2]]
    record.observed_at -= 60_000
    assert tree.evaluate(snapshot, store.records).reason_code == "insufficient_data"


def test_missing_evidence_blocks_direction(market_fixture):
    snapshot, store, tree = market_fixture
    del store.records[snapshot.required_ids[0]]
    assert tree.evaluate(snapshot, store.records).direction == "no_trade"


def test_synthetic_not_accepted_in_live_mode(settings):
    store = EvidenceStore()
    snapshot = demo_snapshot(store)
    assert (
        DecisionTree(settings.tree_file()).evaluate(snapshot, store.records).reason_code
        == "insufficient_data"
    )


def test_bad_tree_field_rejected():
    with pytest.raises(ValidationError):
        Condition(field="__import__('os')", op="eq", value=1)


def test_quality_uses_observation_not_fetch_time():
    fresh = assess_quality("binance", 100_000, 10_000, "ok", at=101_000)
    stale = assess_quality("binance", 1, 10_000, "ok", at=101_000)
    missing = assess_quality("coinglass", None, 10_000, "unavailable", at=101_000)
    future = assess_quality("binance", 120_000, 10_000, "ok", at=101_000)
    assert fresh.score > stale.score
    assert missing.score == future.score == 0


def test_price_zone_rejects_wrong_order_and_nonfinite():
    for values in ({"low": 3, "high": 2}, {"low": 1, "high": float("inf")}):
        with pytest.raises(ValidationError):
            PriceZone(**values)
