from domain.models import Candle


def freshness_factor(observed_at: int | None, ttl_ms: int, at: int) -> tuple[float, str]:
    if observed_at is None:
        factor = 0.5
        reason = "provider observation timestamp unknown"
    elif observed_at > at + 5000:
        factor = 0.0
        reason = "observation timestamp is in the future"
    elif at - observed_at > ttl_ms:
        factor = 0.25
        reason = "stale for this data type"
    else:
        factor = 1.0
        reason = "within data-specific freshness window"
    return factor, reason


def candle_freshness_problems(
    rows: list[Candle], recent_rows: list[Candle], end: int, interval_ms: int
) -> list[str]:
    problems = []
    if not rows or end - rows[-1].close_time > interval_ms:
        problems.append("analysis candles do not cover the latest interval")
    if not recent_rows or end - recent_rows[-1].close_time > 60_000:
        problems.append("recent 1m candles are stale")
    return problems
