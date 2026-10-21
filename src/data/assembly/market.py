from data.catalog import METRICS
from data.processing.indicators import calculate
from domain.models import INTERVAL_MS, Candle, now_ms


def kline_evidence(symbol: str, interval: str, data: dict, *, at: int | None = None) -> dict:
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
    observed = min(rows[-1].close_time, at if at is not None else now_ms()) if rows else None
    return dict(
        tool="get_klines",
        source="binance",
        symbol=symbol,
        observed_at=observed,
        ttl_ms=INTERVAL_MS.get(interval, 2_678_400_000) + 90_000,
        status="partial"
        if data["gaps"] or data["truncated"] or not rows or not data.get("complete", True)
        else "ok",
        summary=summary,
        data=data,
    )


def derivative_evidence(
    symbol: str, name: str, data: dict | list, *, fetched_at: int | None = None
) -> dict:
    spec = METRICS[name]
    complete = True
    if name == "funding_info":
        observed = fetched_at
        summary = {
            "metric": name,
            "adjustment_present": bool(data),
            "rules": data,
            "scope": "only adjusted contracts; absence is normal, no default interval inferred",
        }
    elif isinstance(data, list):
        observed = int(data[-1][spec.time_field]) if data else None
        period_ms = INTERVAL_MS.get(spec.period) if spec.period else None
        gaps = [
            b[spec.time_field]
            for a, b in zip(data, data[1:], strict=False)
            if period_ms and int(b[spec.time_field]) - int(a[spec.time_field]) != period_ms
        ]
        complete = len(data) >= (spec.limit or 1) and not gaps
        summary = {
            "metric": name,
            "period": spec.period,
            "recent": data[-6:],
            "count": len(data),
            "expected_count": spec.limit,
            "gaps": gaps,
            "coverage_start": int(data[0][spec.time_field]) if data else None,
            "coverage_end": observed,
            "scope": "Binance only; provider period-end/settlement timestamps",
        }
    else:
        observed = int(data["time"]) if data.get("time") else None
        summary = {"metric": name, **data}
    return dict(
        tool="get_" + name,
        source="binance",
        symbol=symbol,
        observed_at=observed,
        ttl_ms=spec.ttl_ms,
        summary={**summary, "priority": spec.priority},
        data=data,
        status="ok" if complete and (data or name == "funding_info") else "partial",
    )
