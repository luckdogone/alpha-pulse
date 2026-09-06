from decimal import Decimal

import pytest
from pydantic import ValidationError

from alpha_pulse.decision import Condition, DecisionTree
from alpha_pulse.demo import demo_snapshot
from alpha_pulse.evidence import EvidenceStore, assess_quality
from alpha_pulse.indicators import calculate, ema
from alpha_pulse.models import Candle, Prediction, PriceZone
from alpha_pulse.service import analyze


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


@pytest.mark.parametrize(
    "scenario,direction", [("long", "long"), ("short", "short"), ("ranging", "no_trade")]
)
async def test_demo_contracts(settings, scenario, direction):
    result = await analyze(settings, engine="demo", scenario=scenario)
    assert result.direction == (direction if direction != "no_trade" else "neutral")
    assert result.engine == "demo"
    assert result.status == ("trade" if direction != "no_trade" else "no_trade")
    assert result.valid_until - result.valid_from == 1_800_000
    assert Prediction.model_validate_json(result.model_dump_json()) == result
    if direction == "no_trade":
        assert result.market_regime == "ranging"
        assert result.entry_zone is result.take_profit_zone is result.stop_loss_zone is None
    else:
        assert result.risk_reward_ratio >= 1.5
        for zone in (result.entry_zone, result.take_profit_zone, result.stop_loss_zone):
            for price in (zone.low, zone.high):
                assert Decimal(str(price)) % Decimal("0.001") == 0
        with pytest.raises(ValidationError):
            Prediction.model_validate(result.model_dump() | {"direction": "neutral"})


def test_stale_book_blocks_direction(market_fixture):
    snapshot, store, tree = market_fixture
    record = store.records[snapshot.required_ids[2]]
    record.observed_at -= 60_000
    assert tree.evaluate(snapshot, store).reason_code == "insufficient_data"


def test_missing_evidence_blocks_direction(market_fixture):
    snapshot, store, tree = market_fixture
    del store.records[snapshot.required_ids[0]]
    assert tree.evaluate(snapshot, store).direction == "no_trade"


def test_synthetic_not_accepted_in_live_mode(settings):
    store = EvidenceStore()
    snapshot = demo_snapshot(store)
    assert (
        DecisionTree(settings.tree_file()).evaluate(snapshot, store).reason_code
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
