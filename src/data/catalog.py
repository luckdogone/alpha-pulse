"""Collection windows and refresh cadence; P0 is collection priority, not a trade gate."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSpec:
    path: str
    period: str | None = None
    limit: int | None = None
    refresh_ms: int = 300_000
    ttl_ms: int = 900_000
    priority: str = "P0"
    api_key: bool = False
    time_field: str = "timestamp"


METRICS = {
    "mark_price": MetricSpec(
        "/fapi/v1/premiumIndex", refresh_ms=1000, ttl_ms=60_000, time_field="time"
    ),
    "open_interest": MetricSpec(
        "/fapi/v1/openInterest", refresh_ms=30_000, ttl_ms=60_000, time_field="time"
    ),
    "open_interest_history": MetricSpec("/futures/data/openInterestHist", "5m", 50),
    "open_interest_history_1h": MetricSpec(
        "/futures/data/openInterestHist", "1h", 26, 3_600_000, 7_200_000, "P1"
    ),
    "global_long_short_ratio": MetricSpec("/futures/data/globalLongShortAccountRatio", "5m", 50),
    "top_account_long_short_ratio": MetricSpec(
        "/futures/data/topLongShortAccountRatio", "5m", 50, api_key=True
    ),
    "top_position_long_short_ratio": MetricSpec(
        "/futures/data/topLongShortPositionRatio", "5m", 50, api_key=True
    ),
    "taker_buy_sell_ratio": MetricSpec("/futures/data/takerlongshortRatio", "5m", 50),
    "funding_history": MetricSpec(
        "/fapi/v1/fundingRate",
        limit=30,
        refresh_ms=300_000,
        ttl_ms=86_400_000,
        priority="P1",
        time_field="fundingTime",
    ),
    "funding_info": MetricSpec(
        "/fapi/v1/fundingInfo", refresh_ms=3_600_000, ttl_ms=7_200_000, priority="P2"
    ),
}
