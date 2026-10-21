"""Requests and provider-specific configuration for supplemental data sources."""

import re
from datetime import UTC, datetime, timedelta

from config import Settings
from data.ingestion.transport import JsonHTTP
from domain.models import now_ms

# Exact mapping only: 1000SHIB must not silently become another asset.
COINMETRICS_ASSETS = {"BTC": "btc", "ETH": "eth", "LTC": "ltc", "BCH": "bch", "DOGE": "doge"}


class ExternalProvider:
    def __init__(self, settings: Settings, http: JsonHTTP):
        self.settings, self.http = settings, http

    async def news(self, query: str, hours: int) -> tuple[dict, str, list[str]]:
        # Only keywords are accepted; callers cannot override domain query operators.
        keywords = " ".join(re.findall(r"[\w\u4e00-\u9fff]+", query, re.UNICODE)[:12])
        if not keywords:
            raise ValueError("news query needs keywords")
        domains = [d.strip().lower() for d in self.settings.news_domains.split(",") if d.strip()]
        if not domains or any(not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", d) for d in domains):
            raise ValueError("NEWS_DOMAINS must be a comma-separated domain allowlist")
        query_text = f"{keywords} (" + " OR ".join(f"domainis:{d}" for d in domains) + ")"
        raw = await self.http.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query": query_text,
                "mode": "artlist",
                "format": "json",
                "timespan": f"{hours}h",
                "maxrecords": 20,
                "sort": "datedesc",
            },
        )
        return raw, keywords, domains

    async def liquidations(self, base_asset: str, interval: str, hours: int) -> dict:
        end = now_ms()
        return await self.http.get(
            self.settings.coinglass_base_url.rstrip("/")
            + "/api/futures/liquidation/aggregated-history",
            params={
                "symbol": base_asset,
                "exchange_list": self.settings.coinglass_exchanges,
                "interval": interval,
                "start_time": end - hours * 3_600_000,
                "end_time": end,
                "limit": 1000,
            },
            headers={"CG-API-KEY": self.settings.coinglass_api_key.get_secret_value()},
        )

    async def configured_onchain(self, symbol: str, base_asset: str):
        url = self.settings.onchain_url_template.format(
            symbol=symbol, base_asset=base_asset.lower()
        )
        headers = (
            {"Authorization": "Bearer " + self.settings.onchain_api_key.get_secret_value()}
            if self.settings.onchain_api_key
            else None
        )
        return await self.http.get(url, headers=headers)

    async def coinmetrics(self, asset: str, days: int) -> dict:
        return await self.http.get(
            self.settings.coinmetrics_base_url.rstrip("/") + "/timeseries/asset-metrics",
            params={
                "assets": asset,
                "metrics": "AdrActCnt,TxCnt",
                "frequency": "1d",
                "page_size": days + 1,
                "start_time": (datetime.now(UTC) - timedelta(days=days)).isoformat(),
            },
        )
