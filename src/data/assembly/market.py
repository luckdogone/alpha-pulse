from data.processing.indicators import calculate
from domain.models import INTERVAL_MS, Candle, now_ms


def kline_evidence(symbol: str, interval: str, data: dict) -> dict:
    rows = [Candle.model_validate(r) for r in data["candles"]]
    summary = {k: v for k, v in data.items() if k != "candles"}
    summary.update(count=len(rows), closed_count=sum(c.closed for c in rows))
    if rows:
        summary.update(
            first_open=rows[0].open_time,
            last_close=rows[-1].close,
            last_open=rows[-1].open_time,
            high=max(c.high for c in rows),
            low=min(c.low for c in rows),
            volume=sum(c.volume for c in rows),
            recent=[
                [c.open_time, c.open, c.high, c.low, c.close, c.volume, c.closed] for c in rows[-5:]
            ],
        )
    if sum(c.closed for c in rows) >= 60:
        summary["indicators"] = calculate(rows)
    observed = min(rows[-1].close_time, now_ms()) if rows else None
    return dict(
        tool="get_klines",
        source="binance",
        symbol=symbol,
        observed_at=observed,
        ttl_ms=INTERVAL_MS.get(interval, 2_678_400_000) + 90_000,
        status="partial" if data["gaps"] or data["truncated"] or not rows else "ok",
        summary=summary,
        data=data,
    )


def derivative_evidence(symbol: str, name: str, data: dict | list) -> dict:
    if name not in {"mark_price", "open_interest"}:
        observed = int(data[-1]["timestamp"]) if data else None
        summary = {
            "metric": name,
            "period": "5m",
            "recent": data[-6:],
            "scope": "Binance only; period timestamps are bucket starts",
        }
        ttl = 900_000
    else:
        observed = int(data["time"]) if data.get("time") else None
        summary = {"metric": name, **data}
        ttl = 60_000
    return dict(
        tool="get_" + name,
        source="binance",
        symbol=symbol,
        observed_at=observed,
        ttl_ms=ttl,
        summary=summary,
        data=data,
        status="ok" if data else "partial",
    )
