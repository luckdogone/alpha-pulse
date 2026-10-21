"""Feature assembly joins validated sources on one calibrated snapshot cutoff."""

from data.cleaning.trades import clean_trades
from data.processing.derivatives import funding_features, positioning_features
from data.processing.orderflow import candle_order_flow, volume_delta_divergence
from data.quality.features import FeatureSet, evidence_status

POSITION_SOURCES = {
    "oi_current": ["open_interest"],
    "oi_notional_usdt": ["open_interest_history"],
    **{f"oi_change_{h}": ["open_interest_history"] for h in ("5m", "15m", "1h", "4h")},
    "oi_change_24h": ["open_interest_history_1h"],
    "oi_price_regime": ["open_interest_history"],
    "oi_momentum": ["open_interest_history"],
    "global_long_short_ratio": ["global_long_short_ratio"],
    "global_ls_change_1h": ["global_long_short_ratio"],
    "top_account_ls_ratio": ["top_account_long_short_ratio"],
    "top_position_ls_ratio": ["top_position_long_short_ratio"],
    "top_trader_divergence": ["top_account_long_short_ratio", "top_position_long_short_ratio"],
    "taker_buy_sell_ratio_5m": ["taker_buy_sell_ratio"],
    "crowding_risk_score": ["global_long_short_ratio", "open_interest_history", "funding_history"],
}


def assemble_features(snapshot, records: list, *, p2: bool = False, live: dict | None = None):
    target, recent, book, flow = records[:4]
    by_name = {r.tool.removeprefix("get_"): r for r in records[4:]}
    metrics = {
        name: r.data
        for name, r in by_name.items()
        if r.status in {"ok", "partial"} and r.data is not None and not name.startswith("htf_")
    }
    features = FeatureSet(snapshot.as_of)
    for key, value in snapshot.indicators.items():
        if key not in {"source", "closed_candles", "as_of"}:
            unit = "percent" if key.endswith("pct") else "price_or_ratio"
            features.add(key, value, [target], unit=unit)
    for key, value in candle_order_flow(snapshot.recent_1m).items():
        features.add(
            key, value, [recent], unit="base_asset" if "delta" in key else "price_or_ratio"
        )
    for key in ("cvd_1m", "cvd_5m", "trade_intensity_1m"):
        features.add(
            key,
            flow.summary.get(key),
            [flow],
            unit="events/second" if "intensity" in key else "base_asset",
        )
    trade_data = flow.data or {}
    raw_trades = trade_data.get("trades", [])
    trades = clean_trades(raw_trades) if raw_trades else []
    features.add(
        "volume_delta_divergence",
        volume_delta_divergence(
            snapshot.recent_1m,
            trades,
            trade_data.get("window_start", snapshot.as_of),
            trade_data.get("window_end", snapshot.as_of),
            trade_data.get("complete", False),
        ),
        [recent, flow],
        unit="category",
        method=("price and aggregate delta over the same last closed minute"),
    )
    for key in (
        "best_bid",
        "best_ask",
        "mid_price",
        "spread_bps",
        "bid_depth_usdt",
        "ask_depth_usdt",
        "orderbook_imbalance",
        "depth_change_rate",
        "estimated_slippage_bps",
    ):
        features.add(
            key,
            book.summary.get(key),
            [book],
            unit="bps" if key.endswith("bps") else "price_or_quote_or_ratio",
        )
    if live and live.get("ticker"):
        from data.processing.liquidity import quote_features

        ticker = live["ticker"]
        status = "VALID" if snapshot.as_of - ticker["observed_at"] <= book.ttl_ms else "STALE"
        for key, value in quote_features(ticker).items():
            features.add(
                key,
                value,
                [by_name.get("book_ticker")],
                status=status,
                unit="bps" if key == "spread_bps" else "price",
            )
    for key, value in positioning_features(metrics, snapshot.recent_1m, p2=p2).items():
        dependencies = [by_name.get(name) for name in POSITION_SOURCES[key]]
        if key == "oi_price_regime":
            dependencies.append(recent)
        method = (
            "fractional OI change at exact timestamp horizon" if key.startswith("oi_change") else ""
        )
        if key == "crowding_risk_score":
            method = (
                "uncalibrated mean absolute z-score of ratio/OI/funding, capped at 3 sigma; "
                "0-100, not probability"
            )
        features.add(key, value, dependencies, method=method)
    funding_source = "mark_price"
    stream_mark, rest_mark = by_name.get("mark_price_stream"), by_name.get("mark_price")
    if evidence_status(stream_mark, snapshot.as_of) == "VALID" and (
        not rest_mark or stream_mark.observed_at >= (rest_mark.observed_at or 0)
    ):
        metrics["mark_price"] = stream_mark.data
        funding_source = "mark_price_stream"
    for key, value in funding_features(metrics, snapshot.as_of).items():
        source = (
            "funding_history"
            if key in {"funding_rate_mean_30", "funding_rate_trend"}
            else funding_source
        )
        features.add(
            key,
            value,
            [by_name.get(source)],
            unit="ms"
            if key == "time_to_next_funding"
            else "percent"
            if key.endswith("pct")
            else "fraction",
            method="per-settlement endpoint slope across 30 settled rates"
            if key == "funding_rate_trend"
            else "",
        )
    if p2:
        alignment, htf_records = {}, [target]
        for name, record in by_name.items():
            if name.startswith("htf_"):
                htf_records.append(record)
                interval = name.removeprefix("htf_")
                values = record.summary.get("indicators", {})
                direction = values.get("ema_alignment")
                alignment[interval] = {
                    "direction": direction,
                    "aligned": direction == snapshot.indicators.get("ema_alignment")
                    if direction is not None
                    else None,
                }
                snapshot.higher_timeframes[interval] = (
                    values if evidence_status(record, snapshot.as_of) == "VALID" else {}
                )
        features.add("htf_trend_alignment", alignment or None, htf_records, unit="per_timeframe")
        liquidations = by_name.get("observed_liquidations")
        for suffix, duration in (("5m", 300_000), ("15m", 900_000), ("1h", 3_600_000)):
            data = live or {}
            rows = [
                r
                for r in data.get("liquidations", [])
                if snapshot.as_of - duration < r["time"] <= snapshot.as_of
            ]
            start = data.get("liquidation_coverage_start")
            state = (
                "VALID" if start is not None and start <= snapshot.as_of - duration else "MISSING"
            )
            buy = sum(r["quote"] for r in rows if r["side"] == "BUY")
            sell = sum(r["quote"] for r in rows if r["side"] == "SELL")
            for key, value in {
                "liquidation_buy_usdt": buy,
                "liquidation_sell_usdt": sell,
                "liquidation_imbalance": (buy - sell) / (buy + sell) if buy + sell else 0,
            }.items():
                features.add(
                    f"{key}_{suffix}",
                    value,
                    [liquidations],
                    status=state,
                    unit="quote_asset" if key.endswith("usdt") else "ratio",
                    method="observed sampled forceOrder events only; never exchange-wide totals",
                )
        for key in ("liquidation_buy_usdt", "liquidation_sell_usdt", "liquidation_imbalance"):
            features.add(
                key,
                features.values[f"{key}_5m"],
                [liquidations],
                status=features.status[f"{key}_5m"],
                method="alias of observed 5m window",
            )
    snapshot.features = features.values
    snapshot.feature_quality_status = features.status
    snapshot.feature_metadata = features.metadata
