"""Features derived from supplemental derivatives data."""


def liquidation_totals(rows: list[dict]) -> dict:
    long_usd = sum(float(r["aggregated_long_liquidation_usd"]) for r in rows)
    short_usd = sum(float(r["aggregated_short_liquidation_usd"]) for r in rows)
    return {
        "long_liquidation_usd": long_usd,
        "short_liquidation_usd": short_usd,
        "total_liquidation_usd": long_usd + short_usd,
    }
