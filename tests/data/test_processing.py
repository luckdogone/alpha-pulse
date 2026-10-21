from data.processing.indicators import calculate, ema


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
