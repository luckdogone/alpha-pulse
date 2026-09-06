import asyncio
import json
import logging
import math
from collections.abc import AsyncIterator

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .config import Settings
from .models import INTERVALS, Candle, normalize_symbol, now_ms
from .transport import DataError, JsonHTTP

log = logging.getLogger(__name__)


def number(value, *, positive: bool = False, nonnegative: bool = False) -> float:
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0) or (nonnegative and result < 0):
        raise DataError("non-finite or invalid numeric market value")
    return result


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
            page = [Candle.from_rest(row, at) for row in raw]
            for candle in page:
                if (start_ms is None or candle.open_time >= start_ms) and candle.open_time <= end:
                    by_time[candle.open_time] = candle
            next_cursor = max(c.close_time for c in page) + 1
            if cursor is not None and next_cursor <= cursor:
                raise DataError("Binance kline pagination did not advance")
            cursor = next_cursor
            truncated = start_ms is not None and len(by_time) >= limit and cursor <= end
            if start_ms is None or len(raw) < size or cursor > end:
                break
        rows = sorted(by_time.values(), key=lambda c: c.open_time)
        gaps = [
            b.open_time
            for a, b in zip(rows, rows[1:], strict=False)
            if b.open_time != a.close_time + 1
        ]
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
        bids = sorted(
            [[number(p, positive=True), number(q, nonnegative=True)] for p, q in raw["bids"]],
            reverse=True,
        )
        asks = sorted(
            [[number(p, positive=True), number(q, nonnegative=True)] for p, q in raw["asks"]]
        )
        bids, asks = [r for r in bids if r[1] > 0], [r for r in asks if r[1] > 0]
        if not bids or not asks or bids[0][0] >= asks[0][0] or bids[0][0] <= 0:
            raise DataError("invalid or crossed order book")
        mid = (bids[0][0] + asks[0][0]) / 2
        bid_value = sum(p * q for p, q in bids if p >= mid * 0.995)
        ask_value = sum(p * q for p, q in asks if p <= mid * 1.005)
        total = bid_value + ask_value
        return {
            "observed_at": raw.get("E", raw.get("T")),
            "last_update_id": raw["lastUpdateId"],
            "best_bid": bids[0][0],
            "best_ask": asks[0][0],
            "mid_price": mid,
            "spread_bps": (asks[0][0] - bids[0][0]) / mid * 10000,
            "bid_notional": bid_value,
            "ask_notional": ask_value,
            "imbalance": (bid_value - ask_value) / total if total else 0,
            "coverage": "visible top 100 levels within 0.5% of mid; not the entire book",
            "bids": bids,
            "asks": asks,
        }

    async def trades(self, symbol: str) -> dict:
        raw = await self.get("/fapi/v1/aggTrades", symbol=normalize_symbol(symbol), limit=500)
        if not raw:
            raise DataError("empty aggregate trade sample")
        buy, sell = 0.0, 0.0
        for row in raw:
            if not isinstance(row["m"], bool):
                raise DataError("invalid buyer-maker flag")
            value = number(row["p"], positive=True) * number(row["q"], nonnegative=True)
            if row["m"]:
                sell += value
            else:
                buy += value
        return {
            "observed_at": max(r["T"] for r in raw),
            "sample_start": min(r["T"] for r in raw),
            "sample_count": len(raw),
            "taker_buy_quote": buy,
            "taker_sell_quote": sell,
            "buy_ratio": buy / (buy + sell) if buy + sell else 0.5,
            "coverage": "latest 500 aggregate trades; variable duration; buyer-maker means sell",
            "trades": raw,
        }

    async def stream(self, symbol: str, interval: str) -> AsyncIterator[dict]:
        symbol = normalize_symbol(symbol)
        if interval not in INTERVALS:
            raise ValueError("unsupported interval")
        templates = [self.settings.binance_ws_url_template]
        if self.settings.binance_ws_fallback_url_template:
            templates.append(self.settings.binance_ws_fallback_url_template)
        failures = 0
        last_event_time = 0
        last_closed = -1
        while True:
            template = templates[min(failures, len(templates) - 1)]
            url = template.format(symbol=symbol.lower(), interval=interval)
            try:
                async with connect(
                    url,
                    proxy=self.settings.proxy(binance=True),
                    open_timeout=self.settings.request_timeout_seconds,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=3,
                    max_size=1_000_000,
                    max_queue=16,
                ) as socket:
                    while True:
                        raw = json.loads(await asyncio.wait_for(socket.recv(), timeout=30))
                        event = raw.get("data", raw)
                        if event.get("e") != "kline":
                            continue
                        if event.get("s") != symbol or event["k"].get("i") != interval:
                            continue
                        candle = Candle.from_ws(event["k"])
                        timestamp = int(event["E"])
                        if timestamp < last_event_time or (
                            candle.closed and candle.open_time <= last_closed
                        ):
                            continue
                        if failures:
                            log.info("Kline stream reconnected; next analysis reloads REST history")
                        failures = 0
                        last_event_time = timestamp
                        if candle.closed:
                            last_closed = candle.open_time
                        yield {
                            "symbol": symbol,
                            "interval": interval,
                            "event_time": timestamp,
                            "candle": candle.model_dump(),
                            "source": "binance_ws",
                        }
            except (OSError, TimeoutError, WebSocketException, ValueError, KeyError) as exc:
                failures += 1
                log.warning(
                    "Kline stream interrupted (%s), attempt %d/6", type(exc).__name__, failures
                )
                if failures >= 6:
                    raise DataError("kline stream unavailable after 6 reconnect attempts") from None
                await asyncio.sleep(min(2 ** (failures - 1), 10))
