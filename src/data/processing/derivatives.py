"""Features derived from supplemental derivatives data."""


def liquidation_totals(rows: list[dict]) -> dict:
    long_usd = sum(float(r["aggregated_long_liquidation_usd"]) for r in rows)
    short_usd = sum(float(r["aggregated_short_liquidation_usd"]) for r in rows)
    return {
        "long_liquidation_usd": long_usd,
        "short_liquidation_usd": short_usd,
        "total_liquidation_usd": long_usd + short_usd,
    }


def change_at(
    rows: list[dict], field: str, horizon_ms: int, period_ms: int, *, relative: bool = True
) -> float | None:
    """Require the actual horizon and every intervening bucket."""
    if not rows:
        return None
    end = rows[-1]["timestamp"]
    window = [r for r in rows if end - horizon_ms <= r["timestamp"] <= end]
    expected = horizon_ms // period_ms + 1
    if len(window) != expected or window[0]["timestamp"] != end - horizon_ms:
        return None
    if any(
        b["timestamp"] - a["timestamp"] != period_ms
        for a, b in zip(window, window[1:], strict=False)
    ):
        return None
    first, last = float(window[0][field]), float(window[-1][field])
    return (
        (last - first) / first if relative and first else (last - first if not relative else None)
    )


def positioning_features(metrics: dict, minute_candles: list, *, p2: bool = False) -> dict:
    short = metrics.get("open_interest_history", [])
    long = metrics.get("open_interest_history_1h", [])
    current = metrics.get("open_interest", {})
    result = {
        "oi_current": float(current["openInterest"]) if current else None,
        "oi_notional_usdt": float(short[-1]["sumOpenInterestValue"]) if short else None,
    }
    for label, duration in (
        ("5m", 300_000),
        ("15m", 900_000),
        ("1h", 3_600_000),
        ("4h", 14_400_000),
    ):
        result[f"oi_change_{label}"] = change_at(short, "sumOpenInterest", duration, 300_000)
    result["oi_change_24h"] = change_at(long, "sumOpenInterest", 86_400_000, 3_600_000)
    result["oi_price_regime"] = None
    if short and result["oi_change_15m"] is not None:
        end = short[-1]["timestamp"]

        def price(at):
            rows = [r for r in minute_candles if r.closed and 0 <= at - r.close_time <= 60_000]
            return rows[-1].close if rows else None

        first, last = price(end - 900_000), price(end)
        oi = result["oi_change_15m"]
        if first is not None and last is not None:
            result["oi_price_regime"] = (
                ("price_up" if last > first else "price_down")
                + ("_oi_up" if oi > 0 else "_oi_down")
                if last != first and oi != 0
                else "flat"
            )
    ratios = {
        "global_long_short_ratio": "global_long_short_ratio",
        "top_account_ls_ratio": "top_account_long_short_ratio",
        "top_position_ls_ratio": "top_position_long_short_ratio",
    }
    for feature, metric in ratios.items():
        rows = metrics.get(metric, [])
        result[feature] = float(rows[-1]["longShortRatio"]) if rows else None
    global_rows = metrics.get("global_long_short_ratio", [])
    result["global_ls_change_1h"] = change_at(
        global_rows, "longShortRatio", 3_600_000, 300_000, relative=False
    )
    account = change_at(
        metrics.get("top_account_long_short_ratio", []),
        "longShortRatio",
        3_600_000,
        300_000,
        relative=False,
    )
    position = change_at(
        metrics.get("top_position_long_short_ratio", []),
        "longShortRatio",
        3_600_000,
        300_000,
        relative=False,
    )
    result["top_trader_divergence"] = (
        (account * position < 0)
        if account is not None
        and position is not None
        and metrics["top_account_long_short_ratio"][-1]["timestamp"]
        == metrics["top_position_long_short_ratio"][-1]["timestamp"]
        else None
    )
    taker = metrics.get("taker_buy_sell_ratio", [])
    result["taker_buy_sell_ratio_5m"] = (
        float(taker[-1]["buyVol"]) / float(taker[-1]["sellVol"])
        if taker and float(taker[-1]["sellVol"])
        else None
    )
    if p2:
        latest = change_at(short, "sumOpenInterest", 300_000, 300_000)
        previous = change_at(short[:-1], "sumOpenInterest", 300_000, 300_000)
        result["oi_momentum"] = (
            latest - previous if latest is not None and previous is not None else None
        )
        result["crowding_risk_score"] = crowding_score(
            global_rows, short, metrics.get("funding_history", [])
        )
    return result


def funding_features(metrics: dict, at: int) -> dict:
    mark = metrics.get("mark_price", {})
    history = metrics.get("funding_history", [])
    rates = [float(r["fundingRate"]) for r in history[-30:]]
    return {
        "funding_rate_current": float(mark["lastFundingRate"]) if mark else None,
        "funding_rate_mean_30": sum(rates) / 30 if len(rates) == 30 else None,
        "funding_rate_trend": (rates[-1] - rates[0]) / 29 if len(rates) == 30 else None,
        "time_to_next_funding": int(mark["nextFundingTime"]) - at
        if mark.get("nextFundingTime") and int(mark["nextFundingTime"]) >= at
        else None,
        "mark_index_basis_pct": (float(mark["markPrice"]) / float(mark["indexPrice"]) - 1) * 100
        if mark.get("indexPrice") and float(mark["indexPrice"])
        else None,
    }


def crowding_score(ratios, oi, funding) -> float | None:
    """Uncalibrated 0–100 heuristic: mean absolute z-score, capped at 3 sigma."""
    from statistics import mean, pstdev

    if len(ratios) < 30 or len(oi) < 30 or len(funding) < 30:
        return None
    values = (
        [float(r["longShortRatio"]) for r in ratios],
        [float(r["sumOpenInterest"]) for r in oi],
        [float(r["fundingRate"]) for r in funding],
    )
    scores = []
    for series in values:
        baseline = series[:-1]
        std = pstdev(baseline)
        scores.append(min(abs(series[-1] - mean(baseline)) / std, 3) / 3 if std else 0.0)
    return round(mean(scores) * 100, 2)
