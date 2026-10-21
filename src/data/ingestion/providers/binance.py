from collections.abc import AsyncIterator
from contextlib import aclosing

from config import Settings
from data.cleaning.candles import find_gaps, from_rest, from_ws, merge_page, ordered_candles
from data.errors import DataError
from data.ingestion.transport import JsonHTTP, stream_json
from domain.models import INTERVALS, Candle, normalize_symbol, now_ms

STATISTICS = {
    "open_interest_history": "/futures/data/openInterestHist",
    "global_long_short_ratio": "/futures/data/globalLongShortAccountRatio",
    "top_account_long_short_ratio": "/futures/data/topLongShortAccountRatio",
    "top_position_long_short_ratio": "/futures/data/topLongShortPositionRatio",
    "taker_buy_sell_ratio": "/futures/data/takerlongshortRatio",
}


class Binance:
    def __init__(self, settings: Settings, http: JsonHTTP):
        self.settings = settings
        self.http = http
        self._exchange_info: dict | None = None

    async def get(self, path: str, **params):
        result = await self.http.get(
            self.settings.binance_rest_base_url.rstrip("/") + path,
            params={k: v for k, v in params.items() if v is not None},
        )
        if isinstance(result, dict) and isinstance(result.get("code"), int) and result["code"] < 0:
            raise DataError(f"Binance error code {result['code']}")
        return result

    async def symbol_info(self, symbol: str) -> dict:
        symbol = normalize_symbol(symbol)
        if self._exchange_info is None:
            self._exchange_info = await self.get("/fapi/v1/exchangeInfo")
        for row in self._exchange_info["symbols"]:
            if row["symbol"] == symbol and row["status"] == "TRADING":
                if row.get("contractType") != "PERPETUAL":
                    raise DataError("only trading USD-M perpetual symbols are supported")
                price_filter = next(f for f in row["filters"] if f["filterType"] == "PRICE_FILTER")
                return {
                    "symbol": symbol,
                    "base_asset": row["baseAsset"],
                    "quote_asset": row["quoteAsset"],
                    "tick_size": price_filter["tickSize"],
                }
        raise DataError(
            "unknown or non-trading Binance USD-M symbol; use a full pair, e.g. BTCUSDT"
        )

    async def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> dict:
        symbol = normalize_symbol(symbol)
        if interval not in INTERVALS:
            raise ValueError("unsupported Binance kline interval")
        if not 1 <= limit <= 5000:
            raise ValueError("limit must be between 1 and 5000")
        end = now_ms() if end_ms is None else end_ms
        if end < 0 or end > now_ms() + 5000 or (start_ms is not None and not 0 <= start_ms <= end):
            raise ValueError("invalid UTC millisecond range")
        at = now_ms()
        by_time: dict[int, Candle] = {}
        cursor = start_ms
        # Without startTime Binance returns the latest page, so never paginate that backwards.
        if start_ms is None and limit > 1500:
            raise ValueError("start_ms is required when requesting more than 1500 candles")
        truncated = False
        while len(by_time) < limit:
            size = min(1500, limit - len(by_time))
            raw = await self.get(
                "/fapi/v1/klines",
                symbol=symbol,
                interval=interval,
                startTime=cursor,
                endTime=end,
                limit=size,
            )
            if not isinstance(raw, list):
                raise DataError("invalid Binance kline response")
            if not raw:
                break
            page = [from_rest(row, at) for row in raw]
            merge_page(by_time, page, start_ms, end)
            next_cursor = max(c.close_time for c in page) + 1
            if cursor is not None and next_cursor <= cursor:
                raise DataError("Binance kline pagination did not advance")
            cursor = next_cursor
            truncated = start_ms is not None and len(by_time) >= limit and cursor <= end
            if start_ms is None or len(raw) < size or cursor > end:
                break
        rows = ordered_candles(by_time)
        gaps = find_gaps(rows)
        return {
            "symbol": symbol,
            "interval": interval,
            "requested_start": start_ms,
            "requested_end": end,
            "candles": [c.model_dump() for c in rows],
            "gaps": gaps,
            "truncated": truncated,
            "next_start_ms": cursor if truncated else None,
        }

    async def depth(self, symbol: str) -> dict:
        raw = await self.get("/fapi/v1/depth", symbol=normalize_symbol(symbol), limit=100)
        return raw

    async def trades(self, symbol: str) -> list[dict]:
        raw = await self.get("/fapi/v1/aggTrades", symbol=normalize_symbol(symbol), limit=500)
        return raw

    async def derivative(self, symbol: str, name: str) -> dict | list:
        if name in STATISTICS:
            return await self.get(STATISTICS[name], symbol=symbol, period="5m", limit=12)
        path = {
            "mark_price": "/fapi/v1/premiumIndex",
            "open_interest": "/fapi/v1/openInterest",
        }[name]
        return await self.get(path, symbol=symbol)

    async def stream(self, symbol: str, interval: str) -> AsyncIterator[dict]:
        symbol = normalize_symbol(symbol)
        if interval not in INTERVALS:
            raise ValueError("unsupported interval")
        templates = [self.settings.binance_ws_url_template]
        if self.settings.binance_ws_fallback_url_template:
            templates.append(self.settings.binance_ws_fallback_url_template)
        last_event_time = 0
        last_closed = -1

        def decode(raw: dict) -> dict | None:
            nonlocal last_event_time, last_closed
            event = raw.get("data", raw)
            if event.get("e") != "kline":
                return None
            if event.get("s") != symbol or event["k"].get("i") != interval:
                return None
            candle = from_ws(event["k"])
            timestamp = int(event["E"])
            if timestamp < last_event_time or (candle.closed and candle.open_time <= last_closed):
                return None
            last_event_time = timestamp
            if candle.closed:
                last_closed = candle.open_time
            return {
                "symbol": symbol,
                "interval": interval,
                "event_time": timestamp,
                "candle": candle.model_dump(),
                "source": "binance_ws",
            }

        urls = [t.format(symbol=symbol.lower(), interval=interval) for t in templates]
        async with aclosing(
            stream_json(
                urls,
                proxy=self.settings.proxy(binance=True),
                open_timeout=self.settings.request_timeout_seconds,
                decode=decode,
                stream_name="kline",
            )
        ) as events:
            async for event in events:
                yield event
