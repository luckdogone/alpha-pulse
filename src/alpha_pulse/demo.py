import math

from .evidence import EvidenceStore
from .indicators import calculate
from .models import Candle, Snapshot, now_ms


def demo_snapshot(store: EvidenceStore, scenario: str = "long") -> Snapshot:
    """Synthetic prices solely for offline contract/decision-tree demonstrations."""
    at = now_ms()
    minute = at // 60_000 * 60_000
    direction = -1 if scenario == "short" else 1
    rows = []
    previous = 100.0
    for i in range(251):
        trend = 0.025 * i + 0.00002 * i * i
        wave = 0.25 * math.sin(i * 0.7 + 2.5)
        close = (
            100 + direction * (trend + wave) if scenario != "ranging" else 100 + 0.025 * math.sin(i)
        )
        start = minute - (250 - i) * 60_000
        rows.append(
            Candle(
                open_time=start,
                close_time=start + 59_999,
                open=previous,
                high=max(close, previous) + 0.1,
                low=min(close, previous) - 0.1,
                close=close,
                volume=1000 + i % 7 * 20,
                quote_volume=(1000 + i % 7 * 20) * (close + previous) / 2,
                trades=200,
                taker_buy_volume=600 if direction == 1 else 400,
                closed=i < 250,
            )
        )
        previous = close
    indicators = calculate(rows)
    book = {
        "observed_at": at,
        "mid_price": rows[-1].close,
        "best_bid": rows[-1].close - 0.005,
        "best_ask": rows[-1].close + 0.005,
        "spread_bps": 1,
        "bid_notional": 1_000_000,
        "ask_notional": 800_000,
        "imbalance": 0.2 * direction,
        "coverage": "synthetic demo",
    }
    flow = {
        "observed_at": at,
        "buy_ratio": 0.62 if direction == 1 else 0.38,
        "sample_count": 500,
        "coverage": "synthetic demo",
    }
    derivatives = {
        "mark_price": {"lastFundingRate": "0.0001", "time": at},
        "open_interest": {"openInterest": "10000", "time": at},
    }
    evidence = []
    for tool, summary, data in [
        (
            "get_klines",
            {"interval": "1m", "count": len(rows)},
            {"candles": [c.model_dump() for c in rows]},
        ),
        ("get_recent_1m", {"count": 31}, [c.model_dump() for c in rows[-31:]]),
        ("get_order_book", book, book),
        ("get_order_flow", flow, flow),
        ("get_mark_price", derivatives["mark_price"], derivatives["mark_price"]),
        ("get_open_interest", derivatives["open_interest"], derivatives["open_interest"]),
    ]:
        evidence.append(
            store.add(
                tool=tool,
                source="synthetic",
                symbol="BTCUSDT",
                observed_at=at,
                ttl_ms=60_000,
                summary=summary,
                data=data,
            )
        )
    return Snapshot(
        symbol="BTCUSDT",
        interval="1m",
        as_of=at,
        base_asset="BTC",
        quote_asset="USDT",
        tick_size="0.001",
        candles=rows,
        recent_1m=rows[-31:],
        indicators=indicators,
        order_book=book,
        order_flow=flow,
        derivatives=derivatives,
        evidence_ids=[e.id for e in evidence],
        required_ids=[e.id for e in evidence],
    )
