import pytest

from data.cleaning.trades import clean_trades
from data.processing.derivatives import change_at, funding_features, positioning_features
from data.processing.indicators import calculate
from data.processing.liquidity import calculate_liquidity
from data.processing.orderflow import (
    candle_order_flow,
    rolling_trade_features,
    volume_delta_divergence,
)
from data.quality.features import FeatureSet
from storage.evidence import EvidenceStore


def test_support_resistance_exclude_current_closed_bar(market_fixture):
    rows = [r for r in market_fixture[0].candles if r.closed]
    expected_low = min(r.low for r in rows[-21:-1])
    expected_high = max(r.high for r in rows[-21:-1])
    rows[-1] = rows[-1].model_copy(update={"low": 0.1, "high": 1_000_000})
    result = calculate(rows)
    assert result["support20"] == expected_low and result["resistance20"] == expected_high
    assert result["trade_count_ratio20"] == 1


def test_30_minute_vwap_and_taker_features_require_contiguous_minutes(market_fixture):
    rows = [r for r in market_fixture[0].candles if r.closed][-30:]
    result = candle_order_flow(rows)
    assert result["vwap_30m"] == pytest.approx(
        sum(r.quote_volume for r in rows) / sum(r.volume for r in rows)
    )
    assert result["taker_delta_30m"] == sum(2 * r.taker_buy_volume - r.volume for r in rows)
    assert candle_order_flow(rows[:10] + rows[11:])["vwap_30m"] is None


def test_cvd_uses_base_units_and_exact_time_windows():
    raw = [
        {"a": 1, "p": "100", "q": "10", "T": 1, "m": False},
        {"a": 2, "p": "100", "q": "3", "T": 250_000, "m": False},
        {"a": 3, "p": "100", "q": "1", "T": 300_000, "m": True},
        {"a": 4, "p": "100", "q": "1000", "T": 300_001, "m": False},
    ]
    rows = clean_trades(raw)
    result = rolling_trade_features(rows, 0, 300_000, True)
    assert result == {"cvd_1m": 2, "trade_intensity_1m": 2 / 60, "cvd_5m": 12}
    assert rolling_trade_features(rows, 240_001, 300_000, True)["cvd_1m"] is None
    assert rolling_trade_features(rows, 0, 300_000, False)["cvd_5m"] is None


def test_divergence_aligns_trades_to_closed_candle(market_fixture):
    candle = [r for r in market_fixture[0].candles if r.closed][-1]
    candle = candle.model_copy(update={"open": 100, "close": 101})
    trades = [
        {"timestamp": candle.close_time, "quantity": 2, "buyer_maker": True},
        {"timestamp": candle.close_time + 1, "quantity": 1000, "buyer_maker": False},
    ]
    assert (
        volume_delta_divergence([candle], trades, candle.open_time, candle.close_time + 10, True)
        == "price_up_delta_down"
    )
    assert (
        volume_delta_divergence(
            [candle], trades, candle.open_time + 1, candle.close_time + 10, True
        )
        is None
    )


def test_oi_changes_require_real_horizons_and_no_gaps():
    rows = [{"timestamp": i * 300_000, "sumOpenInterest": str(100 + i)} for i in range(50)]
    assert change_at(rows, "sumOpenInterest", 14_400_000, 300_000) == pytest.approx(48 / 101)
    assert change_at(rows, "sumOpenInterest", 86_400_000, 300_000) is None
    assert change_at(rows[:25] + rows[26:], "sumOpenInterest", 14_400_000, 300_000) is None
    assert (
        change_at(
            [{**r, "sumOpenInterest": "0"} for r in rows], "sumOpenInterest", 300_000, 300_000
        )
        is None
    )


def test_current_and_settled_funding_are_distinct():
    metrics = {
        "mark_price": {
            "lastFundingRate": ".003",
            "markPrice": "101",
            "indexPrice": "100",
            "nextFundingTime": 3000,
        },
        "funding_history": [{"fundingRate": str(i / 10000)} for i in range(30)],
    }
    result = funding_features(metrics, 1000)
    assert result["funding_rate_current"] == 0.003
    assert result["funding_rate_mean_30"] == pytest.approx(0.00145)
    assert result["funding_rate_trend"] == pytest.approx(0.0001)
    assert result["time_to_next_funding"] == 2000
    assert result["mark_index_basis_pct"] == pytest.approx(1)
    metrics["funding_history"].pop()
    assert funding_features(metrics, 1000)["funding_rate_mean_30"] is None
    assert funding_features(metrics, 3001)["time_to_next_funding"] is None


def test_top_trader_divergence_requires_matching_time_windows():
    accounts = [{"timestamp": i * 300_000, "longShortRatio": 1 + i / 100} for i in range(13)]
    positions = [{"timestamp": i * 300_000, "longShortRatio": 1 - i / 100} for i in range(13)]
    metrics = {"top_account_long_short_ratio": accounts, "top_position_long_short_ratio": positions}
    assert positioning_features(metrics, [])["top_trader_divergence"] is True
    metrics["top_position_long_short_ratio"] = [
        {**r, "timestamp": r["timestamp"] - 300_000} for r in positions
    ]
    assert positioning_features(metrics, [])["top_trader_divergence"] is None


def test_slippage_insufficient_depth_and_fixed_band_changes():
    book = {
        "observed_at": 2000,
        "last_update_id": 2,
        "bids": [[99.9, 2], [99, 1]],
        "asks": [[100.1, 3], [101, 1]],
    }
    previous = {**book, "bids": [[99.9, 4], [99, 1]]}
    result = calculate_liquidity(book, notional=100, previous=previous)
    assert result["estimated_slippage_bps"] == pytest.approx({"buy": 10, "sell": 10})
    assert result["depth_change_rate"] == {"bid": -0.5, "ask": 0}
    assert calculate_liquidity(book, notional=1000)["estimated_slippage_bps"] == {
        "buy": None,
        "sell": None,
    }


@pytest.mark.parametrize("bid_edge, ask_edge", [(99.5, 100.5), (99.8, 100.5), (99.5, 100.2)])
def test_depth_coverage_is_per_side_and_band(bid_edge, ask_edge):
    result = calculate_liquidity(
        {
            "observed_at": 2000,
            "last_update_id": 2,
            "bids": [[99.99, 1000], [bid_edge, 1]],
            "asks": [[100.01, 500], [ask_edge, 1]],
        }
    )
    assert result["depth_band_coverage"]["0.1%"] == {"bid": True, "ask": True}
    assert result["depth_band_coverage"]["0.5%"] == {
        "bid": bid_edge == 99.5,
        "ask": ask_edge == 100.5,
    }
    for side, complete in result["depth_band_coverage"]["0.5%"].items():
        observed = result[f"observed_{side}_depth_usdt"]["0.5%"]
        assert observed > 0
        assert result[f"{side}_depth_usdt"]["0.5%"] == (observed if complete else None)
        assert result[f"{side}_notional"] == (observed if complete else None)
    assert (result["imbalance"] is not None) == (bid_edge == 99.5 and ask_edge == 100.5)
    assert result["orderbook_imbalance"] == result["imbalance"]


def test_truncated_band_cannot_publish_reversed_imbalance_but_slippage_is_usable():
    book = {
        "observed_at": 2000,
        "last_update_id": 2,
        "bids": [[99.99, 1000]],
        "asks": [[100.01, 500]],
    }
    partial = calculate_liquidity(book)
    full = calculate_liquidity(
        {
            **book,
            "bids": book["bids"] + [[99.5, 1]],
            "asks": book["asks"] + [[100.2, 2000], [100.5, 1]],
        }
    )
    assert partial["observed_bid_depth_usdt"]["0.5%"] > partial["observed_ask_depth_usdt"]["0.5%"]
    assert partial["imbalance"] is None
    assert full["imbalance"] < 0
    assert partial["estimated_slippage_bps"] == pytest.approx({"buy": 1, "sell": 1})
    assert full["estimated_slippage_bps"] == partial["estimated_slippage_bps"]


@pytest.mark.parametrize("incomplete", ["previous", "current"])
def test_depth_change_requires_both_books_to_cover_the_fixed_band(incomplete):
    book = {
        "observed_at": 2000,
        "last_update_id": 2,
        "bids": [[99.99, 1000], [99.5, 1]],
        "asks": [[100.01, 500], [100.5, 1]],
    }
    partial = {**book, "bids": book["bids"][:1]}
    previous, current = (partial, book) if incomplete == "previous" else (book, partial)
    assert calculate_liquidity(current, previous=previous)["depth_change_rate"] == {
        "bid": None,
        "ask": 0,
    }


@pytest.mark.parametrize(
    "status, observed, expected",
    [
        ("ok", 0, "STALE"),
        ("ok", 2000, "INVALID"),
        ("partial", 1000, "MISSING"),
        ("error", 1000, "INVALID"),
        ("unavailable", None, "MISSING"),
    ],
)
def test_unreliable_features_are_withheld(status, observed, expected):
    record = EvidenceStore().add(
        tool="test",
        source="binance",
        symbol="BTCUSDT",
        observed_at=observed,
        ttl_ms=100,
        summary={},
        status=status,
    )
    features = FeatureSet(1000)
    features.add("example", 123, [record])
    assert features.status["example"] == expected
    assert features.values["example"] is None
    assert features.metadata["example"]["evidence_ids"] == [record.id]
