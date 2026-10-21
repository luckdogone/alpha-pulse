import pytest
from pydantic import ValidationError

from application.demo import demo_snapshot
from domain.decision import Condition, DecisionTree
from domain.models import Candle, PriceZone
from storage.evidence import EvidenceStore


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


def test_price_zone_rejects_wrong_order_and_nonfinite():
    for values in ({"low": 3, "high": 2}, {"low": 1, "high": float("inf")}):
        with pytest.raises(ValidationError):
            PriceZone(**values)
