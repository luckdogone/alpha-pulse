import asyncio
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .evidence import EvidenceStore, assess_quality
from .external import ExternalData
from .market import MarketData
from .models import INTERVALS, Evidence, Model, normalize_symbol
from .transport import DataError


class SymbolArgs(Model):
    symbol: str = Field(description="Full Binance USD-M pair, e.g. BTCUSDT; no bare coin tickers")

    @field_validator("symbol")
    @classmethod
    def symbol_format(cls, value):
        return normalize_symbol(value)


class KlineArgs(SymbolArgs):
    interval: str = Field(description="Binance candle interval, such as 1m, 5m, 1h, 1d, or 1M")
    start_ms: int | None = Field(
        default=None, ge=0, description="Inclusive UTC millisecond open time"
    )
    end_ms: int | None = Field(
        default=None, ge=0, description="Inclusive UTC milliseconds; default now"
    )
    limit: int = Field(
        default=500, ge=1, le=5000, description="Maximum records, with explicit pagination metadata"
    )

    @field_validator("interval")
    @classmethod
    def valid_interval(cls, value):
        if value not in INTERVALS:
            raise ValueError("unsupported Binance interval")
        return value

    @model_validator(mode="after")
    def valid_range(self):
        if self.start_ms is not None and self.end_ms is not None and self.start_ms > self.end_ms:
            raise ValueError("start_ms must not exceed end_ms")
        return self


class NewsArgs(SymbolArgs):
    query: str = Field(
        min_length=2, max_length=180, description="Keywords, preferably coin name plus event topic"
    )
    hours: int = Field(default=24, ge=1, le=168)


class OnchainArgs(SymbolArgs):
    days: int = Field(default=3, ge=1, le=7)


class LiquidationArgs(SymbolArgs):
    interval: Literal["5m", "15m", "30m", "1h", "4h", "12h", "1d"] = "30m"
    hours: int = Field(default=24, ge=1, le=168)


class EvidenceArgs(Model):
    evidence_id: str = Field(pattern=r"^ev_[0-9a-f]{16}$")


class PageArgs(EvidenceArgs):
    offset: int = Field(default=0, ge=0, le=5000)
    limit: int = Field(default=20, ge=1, le=50)
    field: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$",
        description="Optional object key path, e.g. bids, trades, or data.rows",
    )


class NoteArgs(Model):
    note: str = Field(
        max_length=1500,
        description="Working notes: facts with evidence IDs, contradictions and open questions",
    )


TOOL_INFO = {
    "get_klines": (
        KlineArgs,
        "Get history when another timeframe or a precise past interval is needed. "
        "Summary and indicators are returned; inspect raw rows with read_evidence. "
        "Incomplete ranges have next_start_ms. Never infer missing candles.",
    ),
    "get_order_book": (
        SymbolArgs,
        "Refresh visible bids/asks when checking current liquidity or imbalance. "
        "This is a REST snapshot of top 100 levels, not executed buying/selling.",
    ),
    "get_order_flow": (
        SymbolArgs,
        "Refresh the latest 500 aggregate trades when checking actual taker buying/selling. "
        "The sample has variable duration, not a guaranteed 30-minute total.",
    ),
    "get_derivatives": (
        SymbolArgs,
        "Get Binance mark price, funding, open interest, and five-minute ratio histories. "
        "Call for context on positioning or to refresh stale derivatives; Binance coverage only.",
    ),
    "search_news": (
        NewsArgs,
        "Search an allowlist of authoritative media via GDELT when a price/volume anomaly "
        "or possible catalyst needs checking. Returns indexed headlines and original URLs, "
        "not verified article bodies. Zero hits do not mean zero events.",
    ),
    "get_onchain": (
        OnchainArgs,
        "Get daily Coin Metrics active addresses/transactions, or a configured adapter. "
        "Use as background context; daily data cannot explain minute-level changes by itself.",
    ),
    "get_liquidations": (
        LiquidationArgs,
        "Get CoinGlass aggregated long/short liquidations when investigating "
        "a squeeze or leverage flush. Requires a configured key and suitable API plan. "
        "Reports configured exchange coverage and USD units; never substitute Binance-only data.",
    ),
    "assess_evidence": (
        EvidenceArgs,
        "Reassess evidence source, freshness, and completeness before relying on it. "
        "Scores describe data quality, not a probability that an article or forecast is true.",
    ),
    "read_evidence": (
        PageArgs,
        "Read a bounded page of previously fetched raw evidence when its summary is insufficient.",
    ),
    "save_note": (
        NoteArgs,
        "Replace compact working notes to retain facts, contradictions and unresolved questions "
        "across context compaction. Notes are hypotheses, never additional market evidence.",
    ),
}


class ToolRegistry:
    def __init__(
        self, market: MarketData, external: ExternalData, store: EvidenceStore, max_calls: int = 16
    ):
        self.market, self.external, self.store = market, external, store
        self.max_calls, self.calls = max_calls, 0
        self.note = ""

    def definitions(self) -> list[dict]:
        return [
            {"name": name, "description": description, "input_schema": schema.model_json_schema()}
            for name, (schema, description) in sorted(TOOL_INFO.items())
        ]

    async def call(self, name: str, arguments: dict) -> tuple[dict, bool]:
        if self.calls >= self.max_calls:
            return {"status": "error", "error": "tool call budget exhausted"}, True
        self.calls += 1
        if name not in TOOL_INFO:
            return {"status": "error", "error": "unknown tool"}, True
        try:
            args = TOOL_INFO[name][0].model_validate(arguments).model_dump()
            # A whole tool, including its retries/pages, has a bounded wall-clock duration.
            async with asyncio.timeout(45):
                result = await self._dispatch(name, args)
            if isinstance(result, Evidence):
                return result.compact(), result.status in {"unavailable", "error"}
            return result, False
        except (ValueError, KeyError, TypeError, DataError, TimeoutError) as exc:
            message = (
                str(exc)
                if isinstance(exc, DataError)
                else (
                    "invalid tool arguments/data"
                    if not isinstance(exc, TimeoutError)
                    else "tool timed out"
                )
            )
            evidence = self.store.add(
                tool=name,
                source={
                    "search_news": "gdelt",
                    "get_onchain": "coinmetrics",
                    "get_liquidations": "coinglass",
                }.get(name, "binance"),
                symbol=arguments["symbol"][:30]
                if isinstance(arguments, dict) and isinstance(arguments.get("symbol"), str)
                else "unknown",
                observed_at=None,
                ttl_ms=60_000,
                summary={},
                status="error",
                error=message,
            )
            return evidence.compact(), True

    async def _dispatch(self, name: str, args: dict):
        if name == "get_klines":
            return await self.market.klines(**args)
        if name == "get_order_book":
            return await self.market.order_book(**args)
        if name == "get_order_flow":
            return await self.market.order_flow(**args)
        if name == "get_derivatives":
            results = await self.market.derivatives(**args)
            return {"results": [r.compact() for r in results]}
        if name == "search_news":
            return await self.external.news(**args)
        if name in {"get_onchain", "get_liquidations"}:
            info = await self.market.binance.symbol_info(args["symbol"])
            args["base_asset"] = info["base_asset"]
            return await (
                self.external.onchain(**args)
                if name == "get_onchain"
                else self.external.liquidations(**args)
            )
        if name == "assess_evidence":
            item = self.store.records[args["evidence_id"]]
            return {
                "evidence_id": item.id,
                "quality": assess_quality(
                    item.source, item.observed_at, item.ttl_ms, item.status
                ).model_dump(),
            }
        if name == "read_evidence":
            return self.store.page(**args)
        self.note = args["note"]
        return {"status": "ok", "saved_note": self.note}
