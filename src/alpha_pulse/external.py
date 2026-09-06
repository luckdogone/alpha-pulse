import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from .config import Settings
from .evidence import EvidenceStore
from .models import Evidence, now_ms
from .transport import DataError, JsonHTTP


class ExternalData:
    def __init__(self, settings: Settings, http: JsonHTTP, store: EvidenceStore):
        self.settings, self.http, self.store = settings, http, store

    async def news(self, symbol: str, query: str, hours: int = 24) -> Evidence:
        # Only keywords are accepted. Do not let a model override the domain query operators.
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
        if "articles" not in raw:
            raise DataError("GDELT returned no article result envelope")
        articles, seen = [], set()
        for item in raw["articles"]:
            url = item.get("url", "")
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not any(
                host == d or host.endswith("." + d) for d in domains
            ):
                continue
            if url in seen:
                continue
            seen.add(url)
            articles.append(
                {
                    "title": item.get("title", "")[:350],
                    "url": url,
                    "publisher": host,
                    "indexed_at": item.get("seendate"),
                    "published_at": None,
                    "coverage": "search index headline only; article body not verified",
                }
            )
        return self.store.add(
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

    async def liquidations(
        self, symbol: str, base_asset: str, interval: str = "30m", hours: int = 24
    ) -> Evidence:
        if self.settings.coinglass_api_key is None:
            return self.store.add(
                tool="get_liquidations",
                source="coinglass",
                symbol=symbol,
                observed_at=None,
                ttl_ms=3_600_000,
                summary={},
                status="unavailable",
                error="COINGLASS_API_KEY is not configured",
            )
        end = now_ms()
        raw = await self.http.get(
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
        if str(raw.get("code")) != "0":
            raise DataError(
                "CoinGlass rejected request; check key, symbol, and plan interval access"
            )
        data = raw.get("data", [])
        if not isinstance(data, list):
            raise DataError("invalid CoinGlass response")
        long_usd = sum(float(r["aggregated_long_liquidation_usd"]) for r in data)
        short_usd = sum(float(r["aggregated_short_liquidation_usd"]) for r in data)
        return self.store.add(
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
                "long_liquidation_usd": long_usd,
                "short_liquidation_usd": short_usd,
                "total_liquidation_usd": long_usd + short_usd,
                "exchange_list": self.settings.coinglass_exchanges,
                "count": len(data),
                "recent": data[-5:],
                "scope": "CoinGlass aggregation over configured exchanges; not all exchanges",
                "coverage_start": min((int(r["time"]) for r in data), default=None),
                "coverage_end": max((int(r["time"]) for r in data), default=None),
            },
            data=data,
        )

    async def onchain(self, symbol: str, base_asset: str, days: int = 3) -> Evidence:
        if self.settings.onchain_url_template:
            url = self.settings.onchain_url_template.format(
                symbol=symbol, base_asset=base_asset.lower()
            )
            headers = (
                {"Authorization": "Bearer " + self.settings.onchain_api_key.get_secret_value()}
                if self.settings.onchain_api_key
                else None
            )
            raw = await self.http.get(url, headers=headers)
            # Configured adapters must provide their own provider observation timestamp.
            observed = raw.get("observed_at") if isinstance(raw, dict) else None
            if observed is not None and (
                not isinstance(observed, int) or observed < 1_000_000_000_000
            ):
                raise DataError("on-chain adapter observed_at must be UTC milliseconds")
            summary = raw.get("summary", {}) if isinstance(raw, dict) else {}
            if not isinstance(summary, dict):
                raise DataError("on-chain adapter summary must be an object")
            if len(str(summary)) > 4000:
                summary = {"note": "large summary archived; use read_evidence"}
            return self.store.add(
                tool="get_onchain",
                source="configured_onchain",
                symbol=symbol,
                observed_at=observed,
                ttl_ms=172_800_000,
                summary=summary,
                data=raw,
            )
        # Exact mapping only: futures symbols like 1000SHIB must not silently become another asset.
        assets = {"BTC": "btc", "ETH": "eth", "LTC": "ltc", "BCH": "bch", "DOGE": "doge"}
        if base_asset not in assets:
            return self.store.add(
                tool="get_onchain",
                source="coinmetrics",
                symbol=symbol,
                observed_at=None,
                ttl_ms=172_800_000,
                summary={},
                status="unavailable",
                error="no verified Coin Metrics asset mapping; configure ONCHAIN_URL_TEMPLATE",
            )
        raw = await self.http.get(
            self.settings.coinmetrics_base_url.rstrip("/") + "/timeseries/asset-metrics",
            params={
                "assets": assets[base_asset],
                "metrics": "AdrActCnt,TxCnt",
                "frequency": "1d",
                "page_size": days + 1,
                "start_time": (datetime.now(UTC) - timedelta(days=days)).isoformat(),
            },
        )
        data = raw.get("data", [])
        times = [
            int(datetime.fromisoformat(r["time"].replace("Z", "+00:00")).timestamp() * 1000)
            for r in data
        ]
        return self.store.add(
            tool="get_onchain",
            source="coinmetrics",
            symbol=symbol,
            observed_at=max(times, default=None),
            ttl_ms=172_800_000,
            status="ok" if data and not raw.get("next_page_url") else "partial",
            summary={
                "asset": assets[base_asset],
                "frequency": "1d",
                "recent": data,
                "note": "daily active addresses/transaction counts; delayed background data",
            },
            data=data,
        )
