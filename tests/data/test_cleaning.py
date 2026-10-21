from copy import deepcopy

import pytest

from data.cleaning.candles import find_gaps, from_rest, from_ws, merge_page, ordered_candles
from data.cleaning.derivatives import validate_derivative
from data.cleaning.orderbook import clean_order_book
from data.cleaning.trades import clean_trades
from data.errors import DataError
from data.processing.liquidity import calculate_liquidity
from data.processing.orderflow import calculate_order_flow


def test_candles_merge_last_observation_sort_boundaries_and_report_gaps():
    def candle(start, close="101"):
        return from_rest(
            [start, "100", "102", "99", close, "10", start + 59_999, "1000", 3, "6"],
            at=240_000,
        )

    rows = {}
    merge_page(rows, [candle(180_000), candle(60_000), candle(0)], 60_000, 180_000)
    merge_page(rows, [candle(240_000), candle(60_000, "102")], 60_000, 180_000)
    result = ordered_candles(rows)
    assert [r.open_time for r in result] == [60_000, 180_000]
    assert result[0].close == 102
    assert find_gaps(result) == [180_000]
    assert find_gaps([]) == []


def test_rest_and_ws_candles_share_contract_and_close_boundary():
    raw = [0, "100", "102", "99", "101", "10", 59_999, "1000", 3, "6"]
    forming = from_rest(raw, at=59_999)
    closed = from_rest(raw, at=60_000)
    streamed = from_ws(
        {
            "t": 0,
            "T": 59_999,
            "o": "100",
            "h": "102",
            "l": "99",
            "c": "101",
            "v": "10",
            "q": "1000",
            "n": 3,
            "V": "6",
            "x": True,
        }
    )
    assert not forming.closed
    assert closed == streamed


def test_book_cleans_levels_and_limits_liquidity_to_half_percent():
    raw = {
        "T": 123,
        "lastUpdateId": 9,
        "bids": [["98", "1000"], ["100", "0"], ["99.9", "2"]],
        "asks": [["102", "1000"], ["100.1", "3"]],
    }
    original = deepcopy(raw)
    book = clean_order_book(raw)
    result = calculate_liquidity(book)
    assert raw == original
    assert book["bids"] == [[99.9, 2.0], [98.0, 1000.0]]
    assert result["observed_at"] == 123
    assert result["mid_price"] == 100
    assert result["spread_bps"] == pytest.approx(20)
    assert result["bid_notional"] == pytest.approx(199.8)
    assert result["ask_notional"] == pytest.approx(300.3)
    assert result["imbalance"] == pytest.approx((199.8 - 300.3) / 500.1)


@pytest.mark.parametrize(
    "bids, asks",
    [
        ([], [["101", "1"]]),
        ([["100", "0"]], [["101", "1"]]),
        ([["101", "1"]], [["100", "1"]]),
        ([["100", "-1"]], [["101", "1"]]),
    ],
)
def test_invalid_books_are_rejected(bids, asks):
    with pytest.raises(DataError):
        clean_order_book({"lastUpdateId": 1, "bids": bids, "asks": asks})


def test_trade_cleaning_preserves_raw_payload_and_zero_volume_is_neutral():
    raw = [{"p": "10", "q": "0", "T": 2, "m": False}]
    before = deepcopy(raw)
    result = calculate_order_flow(clean_trades(raw))
    assert raw == before
    assert result["buy_ratio"] == 0.5
    assert result["taker_buy_quote"] == result["taker_sell_quote"] == 0


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"p": "10", "q": "1", "T": 1, "m": "false"}],
        [{"p": "inf", "q": "1", "T": 1, "m": False}],
        [{"p": "10", "q": "-1", "T": 1, "m": True}],
    ],
)
def test_invalid_trade_samples_are_rejected(rows):
    with pytest.raises(DataError):
        clean_trades(rows)


@pytest.mark.parametrize(
    "name, raw",
    [
        ("mark_price", {"markPrice": "100", "lastFundingRate": "NaN"}),
        ("open_interest", {"openInterest": "-1"}),
    ],
)
def test_derivative_validation_rejects_invalid_required_metrics(name, raw):
    with pytest.raises(DataError):
        validate_derivative(name, raw)
