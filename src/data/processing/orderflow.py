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
        "coverage": "latest 500 aggregate trades; variable duration; buyer-maker means sell",
    }
