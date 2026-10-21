"""Collection orchestration with calibrated cutoffs and explicit feature validity."""

import asyncio

from config import Settings
from data.assembly.external import coinmetrics_evidence, liquidation_evidence, news_evidence
from data.assembly.market import derivative_evidence, kline_evidence
from data.assembly.snapshot import build_snapshot
from data.catalog import METRICS
from data.cleaning.derivatives import validate_derivative
from data.cleaning.external import clean_liquidations, clean_news, clean_onchain_adapter
from data.cleaning.orderbook import clean_order_book
from data.cleaning.trades import clean_trades
from data.errors import DataError, DataUnavailable
from data.ingestion.providers.binance import Binance
from data.ingestion.providers.external import COINMETRICS_ASSETS, ExternalProvider
from data.ingestion.transport import JsonHTTP
from data.processing.liquidity import calculate_liquidity
from data.processing.orderflow import calculate_order_flow, rolling_trade_features
from domain.models import INTERVAL_MS, Evidence, Snapshot
from storage.evidence import EvidenceStore


class MarketData:
    def __init__(self, settings: Settings, binance: Binance, store: EvidenceStore, live=None):
        self.settings, self.binance, self.store, self.live = settings, binance, store, live

    def save(self, **payload) -> Evidence:
        return self.store.add(**payload, clock_offset_ms=self.binance.clock.offset_ms)

    def failure(self, tool: str, symbol: str, error: Exception) -> Evidence:
        message = (
            str(error) if isinstance(error, DataError) else f"invalid data ({type(error).__name__})"
        )
        return self.save(
            tool=tool,
            source="binance",
            symbol=symbol,
            observed_at=None,
            ttl_ms=30_000,
            summary={},
            status="unavailable" if isinstance(error, DataUnavailable) else "error",
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
        return self.save(**kline_evidence(symbol, interval, data, at=self.binance.clock.now()))

    async def closed_klines(
        self,
        symbol: str,
        interval: str,
        count: int,
        end: int,
        *,
        tool: str = "get_klines",
        padding: int = 2,
        minimum: int | None = None,
    ) -> Evidence:
        data = await self.binance.closed_klines(symbol, interval, count, end=end, padding=padding)
        if minimum is not None:
            data["complete"] = len(data["candles"]) >= minimum and not data["gaps"]
        payload = kline_evidence(symbol, interval, data, at=self.binance.clock.now())
        payload["tool"] = tool
        return self.save(**payload)

    async def order_book(self, symbol: str) -> Evidence:
        view = self.live.view(self.binance.clock.now()) if self.live else {}
        book = view.get("book")
        if (
            book
            and self.binance.clock.now() - book["observed_at"] <= self.settings.realtime_stale_ms
        ):
            data = calculate_liquidity(
                book, self.settings.slippage_notional_usdt, view.get("previous_book")
            )
            data["source"] = "binance_ws_depth"
        else:
            data = calculate_liquidity(
                clean_order_book(await self.binance.depth(symbol)),
                self.settings.slippage_notional_usdt,
            )
            data["source"] = "binance_rest_depth"
        return self.save(
            tool="get_order_book",
            source="binance",
            symbol=symbol,
            observed_at=data["observed_at"],
            ttl_ms=30_000,
            data=data,
            summary={k: v for k, v in data.items() if k not in {"bids", "asks"}},
        )

    async def order_flow(self, symbol: str) -> Evidence:
        view = self.live.view(self.binance.clock.now()) if self.live else {}
        sample = view.get("trades")
        if not sample or not sample["complete"]:
            sample = await self.binance.trades(symbol)
        rows = clean_trades(sample["trades"]) if sample["trades"] else []
        summary = (
            calculate_order_flow(rows)
            if rows
            else {
                "observed_at": sample["window_end"],
                "sample_start": sample["window_start"],
                "sample_count": 0,
                "taker_buy_quote": 0,
                "taker_sell_quote": 0,
                "buy_ratio": None,
                "coverage": "empty time window; no directional trade evidence",
            }
        )
        summary.update({k: v for k, v in sample.items() if k != "trades"})
        summary.update(
            rolling_trade_features(
                rows, sample["window_start"], sample["window_end"], sample["complete"]
            )
        )
        data = {**summary, "trades": sample["trades"]}
        return self.save(
            tool="get_order_flow",
            source="binance",
            symbol=symbol,
            observed_at=sample["window_end"],
            ttl_ms=60_000,
            data=data,
            summary=summary,
            status="ok" if sample["complete"] and rows else "partial",
        )

    async def derivative(self, symbol: str, name: str, *, end_ms: int | None = None) -> Evidence:
        raw = await self.binance.derivative(symbol, name, end_ms=end_ms, force=name == "mark_price")
        data = validate_derivative(name, raw)
        return self.save(
            **derivative_evidence(
                symbol, name, data, fetched_at=self.binance.metric_observed_at.get((symbol, name))
            )
        )

    async def derivatives(self, symbol: str, *, end_ms: int | None = None) -> list[Evidence]:
        names = [
            n
            for n, spec in METRICS.items()
            if spec.priority != "P2" or self.settings.enable_p2_features
        ]
        results = await asyncio.gather(
            *(self.derivative(symbol, n, end_ms=end_ms) for n in names), return_exceptions=True
        )
        return [
            self.failure("get_" + n, symbol, r) if isinstance(r, Exception) else r
            for n, r in zip(names, results, strict=True)
        ]

    async def snapshot(self, symbol: str, interval: str) -> Snapshot:
        if interval not in INTERVAL_MS:
            raise ValueError(
                "analysis requires a fixed interval; 1M is available via get_klines only"
            )
        clock = await self.binance.clock.calibrate(self.binance.get)
        info = await self.binance.symbol_info(symbol)
        symbol, start_cutoff = info["symbol"], self.binance.clock.now()
        tasks = [
            self.closed_klines(
                symbol, interval, self.settings.indicator_warmup_candles, start_cutoff
            ),
            self.closed_klines(
                symbol,
                "1m",
                self.settings.snapshot_minutes + 1,
                start_cutoff,
                tool="get_recent_1m",
                padding=4,
                minimum=self.settings.snapshot_minutes,
            ),
            self.order_book(symbol),
            self.order_flow(symbol),
            self.derivatives(symbol, end_ms=start_cutoff),
        ]
        labels = [
            "get_klines",
            "get_recent_1m",
            "get_order_book",
            "get_order_flow",
            "get_derivatives",
        ]
        if self.settings.enable_p2_features:
            for higher in dict.fromkeys(self.settings.higher_timeframes):
                if INTERVAL_MS[higher] > INTERVAL_MS[interval]:
                    tasks.append(
                        self.closed_klines(
                            symbol,
                            higher,
                            self.settings.higher_timeframe_candles,
                            start_cutoff,
                            tool="get_htf_" + higher,
                        )
                    )
                    labels.append("get_htf_" + higher)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        records: list[Evidence] = []
        for name, value in zip(labels, results, strict=True):
            records.extend(
                [self.failure(name, symbol, value)]
                if isinstance(value, Exception)
                else value
                if isinstance(value, list)
                else [value]
            )
        end = self.binance.clock.now()
        records.extend(
            [
                self.save(
                    tool="get_symbol_info",
                    source="binance",
                    symbol=symbol,
                    observed_at=self.binance.exchange_observed_at,
                    ttl_ms=self.settings.exchange_info_refresh_seconds * 2000,
                    summary=info["trading_rules"],
                    data=info["trading_rules"],
                ),
                self.save(
                    tool="get_server_time",
                    source="binance",
                    symbol=symbol,
                    observed_at=clock["calibrated_at"],
                    ttl_ms=self.settings.clock_refresh_seconds * 2000,
                    summary=clock,
                    data=clock,
                ),
            ]
        )
        live = self.live.view(end) if self.live else None
        # Freeze all live inputs once. Later events cannot change this snapshot's evidence.
        if live:
            for name, value, observed, ttl in (
                (
                    "book_ticker",
                    live["ticker"],
                    live["ticker"]["observed_at"] if live["ticker"] else None,
                    self.settings.realtime_stale_ms,
                ),
                (
                    "mark_price_stream",
                    live["mark"],
                    live["mark"]["time"] if live["mark"] else None,
                    self.settings.realtime_stale_ms,
                ),
                (
                    "observed_liquidations",
                    live["liquidations"],
                    end if live["liquidation_coverage_start"] is not None else None,
                    self.settings.realtime_stale_ms,
                ),
            ):
                if name == "observed_liquidations" and not self.settings.enable_p2_features:
                    continue
                records.append(
                    self.save(
                        tool="get_" + name,
                        source="binance",
                        symbol=symbol,
                        observed_at=observed,
                        ttl_ms=ttl,
                        data=value,
                        summary={
                            "source": "binance_ws",
                            "coverage_start": live["liquidation_coverage_start"]
                            if name == "observed_liquidations"
                            else None,
                        },
                        status="ok" if observed is not None else "unavailable",
                    )
                )
        return build_snapshot(
            info,
            interval,
            end,
            records,
            self.settings.snapshot_minutes,
            warmup=self.settings.indicator_warmup_candles,
            clock=clock,
            p2=self.settings.enable_p2_features,
            live=live,
        )


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
