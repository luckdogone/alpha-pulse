"""Visible depth and simulated fills. Missing depth never means zero execution cost."""


def depth_in_band(book: dict, mid: float, band: float) -> tuple[float, float]:
    return (
        sum(p * q for p, q in book["bids"] if mid * (1 - band) <= p < mid),
        sum(p * q for p, q in book["asks"] if mid < p <= mid * (1 + band)),
    )


def band_coverage(book: dict, mid: float, band: float) -> dict[str, bool]:
    """Only claim coverage through the retained, contiguous price levels."""
    return {
        "bid": book["bids"][-1][0] <= mid * (1 - band),
        "ask": book["asks"][-1][0] >= mid * (1 + band),
    }


def estimated_slippage(book: dict, notional: float, mid: float) -> dict:
    quantity = notional / mid
    result = {}
    for side, levels, sign in (("buy", book["asks"], 1), ("sell", book["bids"], -1)):
        remaining, quote = quantity, 0.0
        for price, size in levels:
            taken = min(remaining, size)
            quote += taken * price
            remaining -= taken
            if remaining <= quantity * 1e-12:
                break
        result[side] = (
            sign * (quote / quantity / mid - 1) * 10_000 if remaining <= quantity * 1e-12 else None
        )
    return result


def calculate_liquidity(book: dict, notional: float = 10_000, previous: dict | None = None) -> dict:
    bids, asks = book["bids"], book["asks"]
    mid = (bids[0][0] + asks[0][0]) / 2
    depths = {
        label: depth_in_band(book, mid, band) for label, band in (("0.1%", 0.001), ("0.5%", 0.005))
    }
    coverage = {
        label: band_coverage(book, mid, band) for label, band in (("0.1%", 0.001), ("0.5%", 0.005))
    }
    bid_value, ask_value = depths["0.5%"]
    total = bid_value + ask_value
    imbalance = (
        ((bid_value - ask_value) / total if total else 0)
        if all(coverage["0.5%"].values())
        else None
    )
    change = None
    if previous:
        anchor = (previous["bids"][0][0] + previous["asks"][0][0]) / 2
        before = depth_in_band(previous, anchor, 0.005)
        after = depth_in_band(book, anchor, 0.005)
        before_coverage = band_coverage(previous, anchor, 0.005)
        after_coverage = band_coverage(book, anchor, 0.005)
        change = {
            side: (b - a) / a if a and before_coverage[side] and after_coverage[side] else None
            for side, a, b in zip(("bid", "ask"), before, after, strict=True)
        }
    return {
        "observed_at": book["observed_at"],
        "event_time": book.get("event_time"),
        "transaction_time": book.get("transaction_time"),
        "last_update_id": book["last_update_id"],
        "best_bid": bids[0][0],
        "best_ask": asks[0][0],
        "mid_price": mid,
        "spread_bps": (asks[0][0] - bids[0][0]) / mid * 10_000,
        "bid_notional": bid_value if coverage["0.5%"]["bid"] else None,
        "ask_notional": ask_value if coverage["0.5%"]["ask"] else None,
        "bid_depth_usdt": {
            label: values[0] if coverage[label]["bid"] else None for label, values in depths.items()
        },
        "ask_depth_usdt": {
            label: values[1] if coverage[label]["ask"] else None for label, values in depths.items()
        },
        "depth_band_coverage": coverage,
        "observed_bid_depth_usdt": {label: values[0] for label, values in depths.items()},
        "observed_ask_depth_usdt": {label: values[1] for label, values in depths.items()},
        "imbalance": imbalance,
        "orderbook_imbalance": imbalance,
        "depth_change_rate": change,
        "estimated_slippage_bps": estimated_slippage(book, notional, mid),
        "slippage_notional_usdt": notional,
        "coverage": (
            "visible levels only; uncovered bands are missing; observed depths are lower bounds; "
            "slippage includes half-spread"
        ),
        "bids": bids,
        "asks": asks,
    }


def quote_features(ticker: dict) -> dict:
    bid, ask = ticker["bid_price"], ticker["ask_price"]
    mid = (bid + ask) / 2
    return {
        "best_bid": bid,
        "best_ask": ask,
        "mid_price": mid,
        "spread_bps": (ask - bid) / mid * 10_000,
    }
