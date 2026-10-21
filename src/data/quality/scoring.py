from data.quality.freshness import freshness_factor
from domain.models import Quality, now_ms


def assess_quality(
    source: str, observed_at: int | None, ttl_ms: int, status: str, at: int | None = None
) -> Quality:
    at = at or now_ms()
    if status in {"unavailable", "error"}:
        return Quality(score=0, level="unavailable", reasons=[status])
    score = {
        "binance": 0.95,
        "derived_binance": 0.9,
        "coinglass": 0.8,
        "coinmetrics": 0.8,
        "gdelt": 0.65,
        "configured_onchain": 0.5,
        "synthetic": 0.0,
    }.get(source, 0.4)
    reasons = [f"source={source}"]
    factor, reason = freshness_factor(observed_at, ttl_ms, at)
    score *= factor
    reasons.append(reason)
    if status == "partial":
        score *= 0.6
        reasons.append("partial coverage")
    return Quality(
        score=round(score, 3),
        level="high" if score >= 0.8 else ("medium" if score >= 0.5 else "low"),
        reasons=reasons,
    )
