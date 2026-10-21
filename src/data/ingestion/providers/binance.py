import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from copy import deepcopy
from time import monotonic

from config import Settings
from data.catalog import METRICS
from data.cleaning.candles import find_gaps, from_rest, from_ws, merge_page, ordered_candles
from data.cleaning.numeric import number
from data.errors import DataError, DataUnavailable
from data.ingestion.clock import ServerClock
from data.ingestion.transport import JsonHTTP, stream_json
from domain.models import INTERVAL_MS, INTERVALS, Candle, normalize_symbol

STATISTICS = {name: spec.path for name, spec in METRICS.items() if spec.period}


class Binance:
    def __init__(self, settings: Settings, http: JsonHTTP):
        self.settings = settings
        self.http = http
        self._exchange_info: dict | None = None
        self._exchange_expires = 0.0
        self.exchange_observed_at: int | None = None
        self.metric_observed_at: dict[tuple[str, str], int] = {}
        self._metric_cache: dict[tuple, tuple[float, object]] = {}
        self._metric_locks: dict[tuple, asyncio.Lock] = {}
        self._closed_cache: dict[tuple, dict] = {}
        self.clock = ServerClock(settings.clock_refresh_seconds, settings.clock_samples)

    async def get(self, path: str, *, api_key: bool = False, **params):
        if api_key and self.settings.binance_api_key is None:
            raise DataUnavailable("BINANCE_API_KEY is required for top-trader ratios")
        result = await self.http.get(
            self.settings.binance_rest_base_url.rstrip("/") + path,
            params={k: v for k, v in params.items() if v is not None},
            headers={"X-MBX-APIKEY": self.settings.binance_api_key.get_secret_value()}
            if api_key
            else None,
        )
        if isinstance(result, dict) and isinstance(result.get("code"), int) and result["code"] < 0:
            raise DataError(f"Binance error code {result['code']}")
        return result

    async def symbol_info(self, symbol: str, *, force: bool = False) -> dict:
        symbol = normalize_symbol(symbol)
        if force or self._exchange_info is None or monotonic() >= self._exchange_expires:
            self._exchange_info = await self.get("/fapi/v1/exchangeInfo")
            self.exchange_observed_at = self.clock.now()
            self._exchange_expires = monotonic() + self.settings.exchange_info_refresh_seconds
        for row in self._exchange_info["symbols"]:
            if row["symbol"] == symbol and row["status"] == "TRADING":
                if row.get("contractType") != "PERPETUAL":
                    raise DataError("only trading USD-M perpetual symbols are supported")
                price_filter = next(
                    (f for f in row["filters"] if f["filterType"] == "PRICE_FILTER"), None
                )
                if price_filter is None:
                    raise DataError("symbol price filter is missing")
                lot = next((f for f in row["filters"] if f["filterType"] == "LOT_SIZE"), {})
                number(price_filter["tickSize"], positive=True)
                if lot:
                    number(lot["stepSize"], positive=True)
                    number(lot["minQty"], positive=True)
                return {
                    "symbol": symbol,
                    "base_asset": row["baseAsset"],
                    "quote_asset": row["quoteAsset"],
                    "tick_size": price_filter["tickSize"],
                    "trading_rules": {
                        **deepcopy(row),
                        "step_size": lot.get("stepSize"),
                        "min_quantity": lot.get("minQty"),
                        "symbol_trading_valid": bool(
                            lot.get("stepSize") and lot.get("minQty") and row.get("marginAsset")
                        ),
                    },
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
        end = self.clock.now() if end_ms is None else end_ms
        if (
            end < 0
            or end > self.clock.now() + 5000
            or (start_ms is not None and not 0 <= start_ms <= end)
        ):
            raise ValueError("invalid UTC millisecond range")
        at = min(self.clock.now(), end + 1)
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

    async def closed_klines(
        self, symbol: str, interval: str, count: int, *, end: int, padding: int = 2
    ) -> dict:
        key = (symbol, interval, count)
        expected_close = end // INTERVAL_MS[interval] * INTERVAL_MS[interval] - 1
        cached = self._closed_cache.get(key)
        if (
            cached
            and cached.get("complete")
            and cached["candles"]
            and cached["candles"][-1]["close_time"] == expected_close
        ):
            return deepcopy(cached)
        data = await self.klines(symbol, interval, end_ms=end, limit=count + padding)
        rows = [
            Candle.model_validate(r)
            for r in data["candles"]
            if r["closed"] and r["close_time"] <= end
        ][-count:]
        data.update(
            candles=[r.model_dump() for r in rows],
            gaps=find_gaps(rows),
            expected_count=count,
            closed_only=True,
            complete=len(rows) >= count and not find_gaps(rows),
        )
        self._closed_cache[key] = deepcopy(data)
        return data

    def ingest_kline(self, symbol: str, interval: str, candle: Candle) -> None:
        if not candle.closed:
            return
        for (cached_symbol, cached_interval, count), data in self._closed_cache.items():
            if cached_symbol != symbol or cached_interval != interval:
                continue
            by_time = {r["open_time"]: Candle.model_validate(r) for r in data["candles"]}
            by_time[candle.open_time] = candle
            rows = ordered_candles(by_time)[-count:]
            data.update(
                candles=[r.model_dump() for r in rows],
                gaps=find_gaps(rows),
                complete=len(rows) >= count and not find_gaps(rows),
            )

    async def depth(self, symbol: str, limit: int | None = None) -> dict:
        return await self.get(
            "/fapi/v1/depth",
            symbol=normalize_symbol(symbol),
            limit=limit or self.settings.depth_limit,
        )

    async def trades(
        self, symbol: str, start_ms: int | None = None, end_ms: int | None = None
    ) -> dict:
        """Page by aggregate ID after the initial time query (never mix both cursors)."""
        end = self.clock.now() if end_ms is None else end_ms
        start = end - self.settings.trade_window_minutes * 60_000 if start_ms is None else start_ms
        if not 0 <= start < end or end - start > 300_000:
            raise ValueError("aggregate trade range must span 1 to 300000 milliseconds")
        rows, cursor, complete = {}, None, False
        for _ in range(self.settings.trade_max_pages):
            params = (
                {"fromId": cursor} if cursor is not None else {"startTime": start, "endTime": end}
            )
            page = await self.get(
                "/fapi/v1/aggTrades", symbol=normalize_symbol(symbol), limit=1000, **params
            )
            if not isinstance(page, list):
                raise DataError("invalid Binance aggregate trade response")
            if not page:
                complete = True
                break
            ordered = sorted(page, key=lambda row: int(row["a"]))
            for row in ordered:
                if start <= int(row["T"]) <= end:
                    rows[int(row["a"])] = row
            next_id = int(ordered[-1]["a"]) + 1
            if cursor is not None and next_id <= cursor:
                raise DataError("Binance aggregate trade pagination did not advance")
            cursor = next_id
            if len(page) < 1000 or int(ordered[-1]["T"]) > end:
                complete = True
                break
        result = sorted(rows.values(), key=lambda row: int(row["a"]))
        gaps = [
            b["a"]
            for a, b in zip(result, result[1:], strict=False)
            if int(b["a"]) != int(a["a"]) + 1
        ]
        return {
            "trades": result,
            "window_start": start,
            "window_end": end,
            "complete": complete and not gaps,
            "truncated": not complete,
            "id_gaps": gaps,
            "next_from_id": cursor if not complete else None,
            "source": "binance_rest",
            "observed_at": end,
        }

    async def derivative(
        self, symbol: str, name: str, *, end_ms: int | None = None, force: bool = False
    ) -> dict | list:
        spec = METRICS[name]
        end = self.clock.now() if end_ms is None else end_ms
        key = (normalize_symbol(symbol), name)
        async with self._metric_locks.setdefault(key, asyncio.Lock()):
            # A new period or funding settlement bypasses the ordinary cache lifetime.
            period_ms = INTERVAL_MS[spec.period] if spec.period else spec.refresh_ms
            bucket = end // period_ms
            cache_key = (*key, bucket)
            cached = self._metric_cache.get(cache_key)
            if cached and monotonic() < cached[0] and not force:
                data = deepcopy(cached[1])
                if isinstance(data, list) and name != "funding_info":
                    data = [r for r in data if int(r[spec.time_field]) <= end]
                return data
            if name == "funding_info":
                raw = await self.get(spec.path)
                if not isinstance(raw, list):
                    raise DataError("invalid funding info response")
                data = [row for row in raw if row.get("symbol") == key[0]]
            else:
                params = {"symbol": key[0]}
                if spec.period:
                    params.update(period=spec.period, limit=spec.limit, endTime=end)
                elif name == "funding_history":
                    params.update(limit=30, endTime=end)
                data = await self.get(spec.path, api_key=spec.api_key, **params)
                rows = data if isinstance(data, list) else [data]
                if any(r.get("symbol", key[0]) != key[0] for r in rows):
                    raise DataError("derivative response symbol mismatch")
                if isinstance(data, list):
                    data = sorted(
                        {
                            int(r[spec.time_field]): r
                            for r in data
                            if int(r[spec.time_field]) <= end
                        }.values(),
                        key=lambda r: int(r[spec.time_field]),
                    )[-spec.limit :]
            # Bound cached periods while retaining independent short/long OI windows.
            self._metric_cache = {k: v for k, v in self._metric_cache.items() if v[0] > monotonic()}
            self._metric_cache[cache_key] = (monotonic() + spec.refresh_ms / 1000, deepcopy(data))
            self.metric_observed_at[key] = self.clock.now()
            return data

    def invalidate_funding_history(self):
        self._metric_cache = {
            key: value for key, value in self._metric_cache.items() if key[1] != "funding_history"
        }

    async def market_stream(
        self, symbol: str, interval: str, route: str, *, on_disconnect=None
    ) -> AsyncIterator[dict]:
        """Subscribe to the correct routed endpoint; lifecycle signals invalidate live state."""
        symbol = normalize_symbol(symbol)
        if interval not in INTERVALS or route not in {"market", "public"}:
            raise ValueError("unsupported stream configuration")
        names = [
            f"{symbol.lower()}@{name}"
            for name in (
                ["depth@500ms", "bookTicker"]
                if route == "public"
                else [
                    *[f"kline_{i}" for i in dict.fromkeys(["1m", interval])],
                    "aggTrade",
                    "markPrice@1s",
                ]
                + (["forceOrder"] if self.settings.enable_p2_features else [])
            )
        ]
        base = (
            self.settings.binance_ws_public_url
            if route == "public"
            else self.settings.binance_ws_market_url
        )

        def decode(raw):
            event = raw.get("data", raw)
            event_symbol = event.get(
                "s", event.get("o", {}).get("s") if isinstance(event.get("o"), dict) else None
            )
            return event if event_symbol == symbol else None

        async with aclosing(
            stream_json(
                [base + "?streams=" + "/".join(names)],
                proxy=self.settings.proxy(binance=True),
                open_timeout=self.settings.request_timeout_seconds,
                decode=decode,
                stream_name=route,
                on_disconnect=on_disconnect,
            )
        ) as events:
            async for event in events:
                yield event

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
