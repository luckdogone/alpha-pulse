from datetime import datetime

from data.processing.derivatives import liquidation_totals


def news_evidence(
    symbol: str, keywords: str, hours: int, domains: list[str], articles: list
) -> dict:
    return dict(
        tool="search_news",
        source="gdelt",
        symbol=symbol,
        observed_at=None,
        ttl_ms=hours * 3_600_000,
        summary={
            "query": keywords,
            "hours": hours,
            "articles": articles[:8],
            "count": len(articles),
            "allowed_domains": domains,
            "note": "no hits does not prove no news; index time is not publication time",
        },
        data=articles,
    )


def liquidation_evidence(
    symbol: str, base_asset: str, interval: str, hours: int, exchanges: str, data: list
) -> dict:
    totals = liquidation_totals(data)
    return dict(
        tool="get_liquidations",
        source="coinglass",
        symbol=symbol,
        observed_at=max((int(r["time"]) for r in data), default=None),
        ttl_ms=max(
            3_600_000, {"4h": 4, "12h": 12, "1d": 24}.get(interval, 1) * 3_600_000 + 300_000
        ),
        status="partial" if len(data) >= 1000 or not data else "ok",
        summary={
            "base_asset": base_asset,
            "interval": interval,
            "hours": hours,
            **totals,
            "exchange_list": exchanges,
            "count": len(data),
            "recent": data[-5:],
            "scope": "CoinGlass aggregation over configured exchanges; not all exchanges",
            "coverage_start": min((int(r["time"]) for r in data), default=None),
            "coverage_end": max((int(r["time"]) for r in data), default=None),
        },
        data=data,
    )


def coinmetrics_evidence(symbol: str, asset: str, raw: dict) -> dict:
    data = raw.get("data", [])
    times = [
        int(datetime.fromisoformat(r["time"].replace("Z", "+00:00")).timestamp() * 1000)
        for r in data
    ]
    return dict(
        tool="get_onchain",
        source="coinmetrics",
        symbol=symbol,
        observed_at=max(times, default=None),
        ttl_ms=172_800_000,
        status="ok" if data and not raw.get("next_page_url") else "partial",
        summary={
            "asset": asset,
            "frequency": "1d",
            "recent": data,
            "note": "daily active addresses/transaction counts; delayed background data",
        },
        data=data,
    )
