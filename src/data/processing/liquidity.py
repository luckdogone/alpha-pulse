"""Liquidity features over the cleaned visible order book."""


def calculate_liquidity(book: dict) -> dict:
    bids, asks = book["bids"], book["asks"]
    mid = (bids[0][0] + asks[0][0]) / 2
    bid_value = sum(p * q for p, q in bids if p >= mid * 0.995)
    ask_value = sum(p * q for p, q in asks if p <= mid * 1.005)
    total = bid_value + ask_value
    return {
        "observed_at": book["observed_at"],
        "last_update_id": book["last_update_id"],
        "best_bid": bids[0][0],
        "best_ask": asks[0][0],
        "mid_price": mid,
        "spread_bps": (asks[0][0] - bids[0][0]) / mid * 10000,
        "bid_notional": bid_value,
        "ask_notional": ask_value,
        "imbalance": (bid_value - ask_value) / total if total else 0,
        "coverage": "visible top 100 levels within 0.5% of mid; not the entire book",
        "bids": bids,
        "asks": asks,
    }
