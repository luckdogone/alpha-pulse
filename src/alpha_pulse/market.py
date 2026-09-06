import asyncio

from .binance import Binance, number
from .config import Settings
from .evidence import EvidenceStore
from .indicators import calculate
from .models import INTERVAL_MS, Candle, Evidence, Snapshot, now_ms
from .transport import DataError

STATISTICS = {
    "open_interest_history": "/futures/data/openInterestHist",
    "global_long_short_ratio": "/futures/data/globalLongShortAccountRatio",
    "top_account_long_short_ratio": "/futures/data/topLongShortAccountRatio",
    "top_position_long_short_ratio": "/futures/data/topLongShortPositionRatio",
    "taker_buy_sell_ratio": "/futures/data/takerlongshortRatio",
}


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
        rows = [Candle.model_validate(r) for r in data["candles"]]
        summary = {k: v for k, v in data.items() if k != "candles"}
        summary.update(count=len(rows), closed_count=sum(c.closed for c in rows))
        if rows:
            summary.update(
                first_open=rows[0].open_time,
                last_close=rows[-1].close,
                last_open=rows[-1].open_time,
                high=max(c.high for c in rows),
                low=min(c.low for c in rows),
                volume=sum(c.volume for c in rows),
                recent=[
                    [c.open_time, c.open, c.high, c.low, c.close, c.volume, c.closed]
                    for c in rows[-5:]
                ],
            )
        if sum(c.closed for c in rows) >= 60:
            summary["indicators"] = calculate(rows)
        observed = min(rows[-1].close_time, now_ms()) if rows else None
        return self.store.add(
            tool="get_klines",
            source="binance",
            symbol=symbol,
            observed_at=observed,
            ttl_ms=INTERVAL_MS.get(interval, 2_678_400_000) + 90_000,
            status="partial" if data["gaps"] or data["truncated"] or not rows else "ok",
            summary=summary,
            data=data,
        )

    async def order_book(self, symbol: str) -> Evidence:
        data = await self.binance.depth(symbol)
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
        data = await self.binance.trades(symbol)
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
        if name in STATISTICS:
            data = await self.binance.get(STATISTICS[name], symbol=symbol, period="5m", limit=12)
            observed = int(data[-1]["timestamp"]) if data else None
            summary = {
                "metric": name,
                "period": "5m",
                "recent": data[-6:],
                "scope": "Binance only; period timestamps are bucket starts",
            }
            ttl = 900_000
        else:
            path = {
                "mark_price": "/fapi/v1/premiumIndex",
                "open_interest": "/fapi/v1/openInterest",
            }[name]
            data = await self.binance.get(path, symbol=symbol)
            if name == "mark_price":
                number(data["lastFundingRate"])
                number(data["markPrice"], positive=True)
            else:
                number(data["openInterest"], nonnegative=True)
            observed = int(data["time"]) if data.get("time") else None
            summary = {"metric": name, **data}
            ttl = 60_000
        return self.store.add(
            tool="get_" + name,
            source="binance",
            symbol=symbol,
            observed_at=observed,
            ttl_ms=ttl,
            summary=summary,
            data=data,
            status="ok" if data else "partial",
        )

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
        target, recent, book, flow = records[:4]
        rows = [Candle.model_validate(r) for r in (target.data or {}).get("candles", [])]
        recent_rows = [Candle.model_validate(r) for r in (recent.data or {}).get("candles", [])]
        problems = []
        try:
            indicators = calculate(rows)
        except ValueError as exc:
            indicators = {}
            problems.append(str(exc))
        if len([r for r in recent_rows if r.closed]) < self.settings.snapshot_minutes:
            problems.append("missing candles in the recent half-hour window")
        if not rows or end - rows[-1].close_time > INTERVAL_MS[interval]:
            problems.append("analysis candles do not cover the latest interval")
        if not recent_rows or end - recent_rows[-1].close_time > 60_000:
            problems.append("recent 1m candles are stale")
        required = records[:4] + [
            r for r in records[4:] if r.tool in {"get_mark_price", "get_open_interest"}
        ]
        if len(required) != 6:
            problems.append("required derivatives missing")
        for r in required:
            if r.status != "ok":
                problems.append(f"required data {r.tool}: {r.status}")
        return Snapshot(
            **info,
            interval=interval,
            as_of=end,
            candles=rows,
            recent_1m=recent_rows,
            indicators=indicators,
            order_book=book.summary,
            order_flow=flow.summary,
            derivatives={r.tool.removeprefix("get_"): r.summary for r in records[4:]},
            evidence_ids=[r.id for r in records],
            required_ids=[r.id for r in required],
            problems=problems,
        )
