"""Taker flow over a variable-duration sample of normalized aggregate trades."""


def calculate_order_flow(rows: list[dict]) -> dict:
    buy, sell = 0.0, 0.0
    for row in rows:
        value = row["price"] * row["quantity"]
        if row["buyer_maker"]:
            sell += value
        else:
            buy += value
    return {
        "observed_at": max(r["timestamp"] for r in rows),
        "sample_start": min(r["timestamp"] for r in rows),
        "sample_count": len(rows),
        "taker_buy_quote": buy,
        "taker_sell_quote": sell,
        "buy_ratio": buy / (buy + sell) if buy + sell else 0.5,
        "coverage": "aggregate trades in the declared time window; buyer-maker means sell",
    }


def candle_order_flow(rows, *, minutes: int = 30) -> dict:
    """Base-asset ratios/deltas and quote/base VWAP over consecutive closed minute bars."""
    from data.cleaning.candles import find_gaps

    rows = [r for r in rows if r.closed]
    result = {}
    for size, suffix in ((1, "1m"), (minutes, "30m")):
        window = rows[-size:]
        valid = (
            len(window) == size
            and not find_gaps(window)
            and all(r.close_time - r.open_time == 59_999 for r in window)
        )
        volume = sum(r.volume for r in window)
        buy = sum(r.taker_buy_volume for r in window)
        result[f"taker_buy_ratio_{suffix}"] = buy / volume if valid and volume else None
        result[f"taker_delta_{suffix}"] = 2 * buy - volume if valid else None
        if size == minutes:
            result["vwap_30m"] = (
                sum(r.quote_volume for r in window) / volume if valid and volume else None
            )
    return result


def rolling_trade_features(rows: list[dict], start: int, end: int, complete: bool) -> dict:
    """CVD is base volume, intensity counts aggregate events (not underlying fills)."""
    result = {}
    for duration, suffix in ((60_000, "1m"), (300_000, "5m")):
        window = [r for r in rows if end - duration < r["timestamp"] <= end]
        valid = complete and start <= end - duration
        ids = [r["id"] for r in window]
        valid = (
            valid
            and all(i is not None for i in ids)
            and all(b == a + 1 for a, b in zip(ids, ids[1:], strict=False))
        )
        result[f"cvd_{suffix}"] = (
            sum(r["quantity"] * (-1 if r["buyer_maker"] else 1) for r in window) if valid else None
        )
        if suffix == "1m":
            result["trade_intensity_1m"] = len(window) / 60 if valid else None
    return result


def volume_delta_divergence(
    candles, trades: list[dict], start: int, end: int, complete: bool
) -> str | None:
    rows = [r for r in candles if r.closed and r.close_time <= end]
    if not rows or not complete:
        return None
    candle = rows[-1]
    if start > candle.open_time or end - candle.close_time > 60_000:
        return None
    delta = sum(
        r["quantity"] * (-1 if r["buyer_maker"] else 1)
        for r in trades
        if candle.open_time <= r["timestamp"] <= candle.close_time
    )
    change = candle.close - candle.open
    if change > 0 and delta < 0:
        return "price_up_delta_down"
    if change < 0 and delta > 0:
        return "price_down_delta_up"
    return "aligned_or_flat"
