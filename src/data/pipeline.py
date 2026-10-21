"""Orchestrate collection, normalization, features, assembly and evidence persistence."""

import asyncio

from config import Settings
from data.assembly.external import coinmetrics_evidence, liquidation_evidence, news_evidence
from data.assembly.market import derivative_evidence, kline_evidence
from data.assembly.snapshot import build_snapshot
from data.cleaning.derivatives import validate_derivative
from data.cleaning.external import clean_liquidations, clean_news, clean_onchain_adapter
from data.cleaning.orderbook import clean_order_book
from data.cleaning.trades import clean_trades
from data.errors import DataError
from data.ingestion.providers.binance import STATISTICS, Binance
from data.ingestion.providers.external import COINMETRICS_ASSETS, ExternalProvider
from data.ingestion.transport import JsonHTTP
from data.processing.liquidity import calculate_liquidity
from data.processing.orderflow import calculate_order_flow
from domain.models import INTERVAL_MS, Evidence, Snapshot, now_ms
from storage.evidence import EvidenceStore


class MarketData:
    def __init__(self, settings: Settings, binance: Binance, store: EvidenceStore):
        self.settings, self.binance, self.store = settings, binance, store

    def failure(self, tool: str, symbol: str, error: Exception) -> Evidence:
        message = (
            str(error) if isinstance(error, DataError) else f"invalid data ({type(error).__name__})"
        )
        return self.store.add(
            tool=tool,
            source="binance",
            symbol=symbol,
            observed_at=None,
            ttl_ms=30_000,
            summary={},
            status="error",
            error=message,
        )

    async def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> Evidence:
        data = await self.binance.klines(symbol, interval, start_ms, end_ms, limit)
        return self.store.add(**kline_evidence(symbol, interval, data))

    async def order_book(self, symbol: str) -> Evidence:
        raw = await self.binance.depth(symbol)
        data = calculate_liquidity(clean_order_book(raw))
        return self.store.add(
            tool="get_order_book",
            source="binance",
            symbol=symbol,
            observed_at=data["observed_at"],
            ttl_ms=30_000,
            data=data,
            summary={k: v for k, v in data.items() if k not in {"bids", "asks"}},
        )

    async def order_flow(self, symbol: str) -> Evidence:
        raw = await self.binance.trades(symbol)
        data = {**calculate_order_flow(clean_trades(raw)), "trades": raw}
        return self.store.add(
            tool="get_order_flow",
            source="binance",
            symbol=symbol,
            observed_at=data["observed_at"],
            ttl_ms=60_000,
            data=data,
            summary={k: v for k, v in data.items() if k != "trades"},
        )

    async def derivative(self, symbol: str, name: str) -> Evidence:
        raw = await self.binance.derivative(symbol, name)
        data = validate_derivative(name, raw)
        return self.store.add(**derivative_evidence(symbol, name, data))

    async def derivatives(self, symbol: str) -> list[Evidence]:
        names = ["mark_price", "open_interest", *STATISTICS]
        results = await asyncio.gather(
            *(self.derivative(symbol, n) for n in names), return_exceptions=True
        )
        return [
            self.failure("get_" + n, symbol, r) if isinstance(r, Exception) else r
            for n, r in zip(names, results, strict=True)
        ]

    async def snapshot(self, symbol: str, interval: str) -> Snapshot:
        info = await self.binance.symbol_info(symbol)
        symbol = info["symbol"]
        if interval not in INTERVAL_MS:
            raise ValueError(
                "analysis requires a fixed interval; 1M is available via get_klines only"
            )
        server_time = int((await self.binance.get("/fapi/v1/time"))["serverTime"])
        if abs(server_time - now_ms()) > 5000:
            raise DataError("local clock differs from Binance by more than 5 seconds")
        # Independently load at least half an hour of 1m candles for all analysis intervals.
        end = server_time
        start = (end // 60_000 - self.settings.snapshot_minutes) * 60_000
        tasks = [
            self.klines(
                symbol, interval, end_ms=end, limit=self.settings.indicator_warmup_candles + 1
            ),
            self.klines(
                symbol, "1m", start_ms=start, end_ms=end, limit=self.settings.snapshot_minutes + 1
            ),
            self.order_book(symbol),
            self.order_flow(symbol),
            self.derivatives(symbol),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        labels = [
            "get_klines",
            "get_recent_1m",
            "get_order_book",
            "get_order_flow",
            "get_derivatives",
        ]
        records: list[Evidence] = []
        for name, value in zip(labels, results, strict=True):
            if isinstance(value, Exception):
                records.append(self.failure(name, symbol, value))
            elif isinstance(value, list):
                records.extend(value)
            else:
                records.append(value)
        return build_snapshot(info, interval, end, records, self.settings.snapshot_minutes)


class ExternalData:
    """Collect and assemble optional news, liquidation and on-chain evidence."""

    def __init__(self, settings: Settings, http: JsonHTTP, store: EvidenceStore):
        self.settings, self.store = settings, store
        self.provider = ExternalProvider(settings, http)

    async def news(self, symbol: str, query: str, hours: int = 24) -> Evidence:
        raw, keywords, domains = await self.provider.news(query, hours)
        articles = clean_news(raw, domains)
        return self.store.add(**news_evidence(symbol, keywords, hours, domains, articles))

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
        raw = await self.provider.liquidations(base_asset, interval, hours)
        data = clean_liquidations(raw)
        return self.store.add(
            **liquidation_evidence(
                symbol, base_asset, interval, hours, self.settings.coinglass_exchanges, data
            )
        )

    async def onchain(self, symbol: str, base_asset: str, days: int = 3) -> Evidence:
        if self.settings.onchain_url_template:
            raw = await self.provider.configured_onchain(symbol, base_asset)
            observed, summary = clean_onchain_adapter(raw)
            return self.store.add(
                tool="get_onchain",
                source="configured_onchain",
                symbol=symbol,
                observed_at=observed,
                ttl_ms=172_800_000,
                summary=summary,
                data=raw,
            )
        if base_asset not in COINMETRICS_ASSETS:
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
        asset = COINMETRICS_ASSETS[base_asset]
        raw = await self.provider.coinmetrics(asset, days)
        return self.store.add(**coinmetrics_evidence(symbol, asset, raw))
