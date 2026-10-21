"""Continuous market/public subscriptions with bounded buffering and REST recovery."""

import asyncio
from collections import deque
from contextlib import aclosing
from time import monotonic

from data.catalog import METRICS
from data.cleaning.candles import from_ws
from data.cleaning.numeric import number
from data.errors import DataError, SequenceError
from data.ingestion.state import OrderBookState, TradeWindow
from domain.models import normalize_symbol


class LiveMarket:
    def __init__(self, binance, symbol: str, interval: str):
        self.binance, self.settings = binance, binance.settings
        self.symbol, self.interval = normalize_symbol(symbol), interval
        self.book = OrderBookState(100)
        self.trades = TradeWindow(self.settings.trade_window_minutes)
        self.tickers: deque[dict] = deque(maxlen=256)
        self.marks: deque[dict] = deque(maxlen=120)
        self.liquidations: dict[tuple, dict] = {}
        self.market_since: int | None = None
        self.received = {"public": 0, "market": 0}
        self.generations = {"public": 0, "market": 0}
        self.errors: dict[str, str] = {}
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
        self.closes: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.tasks: list[asyncio.Task] = []
        self._last_closed: dict[str, int] = {}
        self._next_depth_sync = 0.0
        self._backfill_lock = asyncio.Lock()
        self._market_inflight = False

    def disconnected(self, route: str):
        self.received[route] = 0
        self.generations[route] += 1
        if route == "public":
            self.book.invalidate()
            self.tickers.clear()
        else:
            self.trades.invalidate()
            self.marks.clear()
            self.market_since = None

    def healthy(self, route: str, at: int) -> bool:
        return 0 <= at - self.received[route] <= self.settings.realtime_stale_ms

    async def __aenter__(self):
        await self.binance.clock.calibrate(self.binance.get)
        self.tasks = [asyncio.create_task(self._produce(route)) for route in ("market", "public")]
        try:
            await self.backfill()
        except (DataError, ValueError, KeyError, TypeError) as exc:
            self.errors["trades"] = type(exc).__name__
        except BaseException:
            await self.__aexit__()
            raise
        self.tasks.extend(
            [asyncio.create_task(self._consume()), asyncio.create_task(self._refresh())]
        )
        return self

    async def __aexit__(self, *args):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.disconnected("market")
        self.disconnected("public")

    async def _produce(self, route: str):
        try:
            async with aclosing(
                self.binance.market_stream(
                    self.symbol,
                    self.interval,
                    route,
                    on_disconnect=lambda: self.disconnected(route),
                )
            ) as events:
                async for event in events:
                    await self.queue.put((route, self.generations[route], event))
        except (DataError, ValueError, KeyError, TypeError) as exc:
            self.disconnected(route)
            self.errors[route] = type(exc).__name__
            if route == "market":
                if self.closes.full():
                    self.closes.get_nowait()
                self.closes.put_nowait(DataError("market stream unavailable"))

    async def backfill(self):
        async with self._backfill_lock:
            generation = self.generations["market"]
            end = self.binance.clock.now()
            data = await self.binance.trades(self.symbol, end_ms=end)
            if generation != self.generations["market"]:
                return
            self.trades.seed(data)
            self.errors.pop("trades", None)

    async def _consume(self):
        while True:
            route, generation, event = await self.queue.get()
            if generation != self.generations[route]:
                continue
            self._market_inflight = route == "market"
            try:
                await self.apply(route, event)
            except (DataError, ValueError, KeyError, TypeError) as exc:
                self.errors[event.get("e", route)] = type(exc).__name__
                if event.get("e") == "depthUpdate":
                    self.book.invalidate()
                if event.get("e") == "aggTrade":
                    self.trades.invalidate()
            finally:
                self._market_inflight = False

    async def apply(self, route: str, event: dict):
        generation = self.generations[route]
        at = self.binance.clock.now()
        timestamp = int(event["E"])
        if timestamp > at + 5000:
            raise DataError("future websocket timestamp")
        self.received[route] = max(self.received[route], timestamp)
        if route == "market" and self.market_since is None:
            self.market_since = timestamp
        kind = event["e"]
        if kind == "depthUpdate":
            if self.book.update_id is None:
                if monotonic() < self._next_depth_sync:
                    return
                self._next_depth_sync = monotonic() + 1
                # Producers continue buffering while REST is in flight.
                self.book.seed(await self.binance.depth(self.symbol, limit=500))
                if generation != self.generations[route]:
                    self.book.invalidate()
                    return
            try:
                self.book.apply(event)
            except SequenceError:
                self.book.invalidate()
                raise
        elif kind == "aggTrade":
            from data.cleaning.trades import clean_trades

            clean_trades([event])
            if self.trades.add(event):
                await self.backfill()
        elif kind == "bookTicker":
            bid, ask = number(event["b"], positive=True), number(event["a"], positive=True)
            if bid >= ask:
                raise DataError("crossed book ticker")
            row = {
                "observed_at": timestamp,
                "bid_price": bid,
                "ask_price": ask,
                "bid_quantity": number(event["B"], nonnegative=True),
                "ask_quantity": number(event["A"], nonnegative=True),
                "update_id": int(event["u"]),
            }
            if not self.tickers or timestamp >= self.tickers[-1]["observed_at"]:
                self.tickers.append(row)
        elif kind == "markPriceUpdate":
            if self.marks and timestamp < self.marks[-1]["time"]:
                return
            if self.marks and int(event["T"]) != self.marks[-1]["nextFundingTime"]:
                self.binance.invalidate_funding_history()
            self.marks.append(
                {
                    "markPrice": number(event["p"], positive=True),
                    "indexPrice": number(event["i"], positive=True),
                    "lastFundingRate": number(event["r"]),
                    "nextFundingTime": int(event["T"]),
                    "time": timestamp,
                }
            )
        elif kind == "kline":
            candle, interval = from_ws(event["k"]), event["k"]["i"]
            if candle.closed and candle.close_time > timestamp:
                raise DataError("closed candle ends after its event time")
            self.binance.ingest_kline(self.symbol, interval, candle)
            if candle.closed and candle.open_time > self._last_closed.get(interval, -1):
                self._last_closed[interval] = candle.open_time
                if interval == self.interval:
                    if self.closes.full():
                        self.closes.get_nowait()
                    self.closes.put_nowait(event)
        elif kind == "forceOrder":
            order = event["o"]
            if order["S"] not in {"BUY", "SELL"}:
                raise DataError("invalid liquidation side")
            filled = number(order["z"], nonnegative=True)
            if filled:
                price = number(order["ap"], positive=True)
                key = (order["T"], order["S"], order["p"], order["q"])
                self.liquidations[key] = {
                    "time": int(order["T"]),
                    "side": order["S"],
                    "quote": price * filled,
                    "status": order["X"],
                    "raw": order,
                }
            self.liquidations = {
                k: r for k, r in self.liquidations.items() if r["time"] >= at - 3_600_000
            }

    async def _refresh(self):
        while True:
            try:
                await self.binance.clock.calibrate(self.binance.get)
                await self.binance.symbol_info(self.symbol)
                names = [
                    n
                    for n, spec in METRICS.items()
                    if spec.priority != "P2" or self.settings.enable_p2_features
                ]
                await asyncio.gather(
                    *(self.binance.derivative(self.symbol, n) for n in names),
                    return_exceptions=True,
                )
                end = self.binance.clock.now()
                await asyncio.gather(
                    self.binance.closed_klines(
                        self.symbol, self.interval, self.settings.indicator_warmup_candles, end=end
                    ),
                    self.binance.closed_klines(
                        self.symbol, "1m", self.settings.snapshot_minutes + 1, end=end, padding=4
                    ),
                )
                if not self.trades.complete:
                    await self.backfill()
            except (DataError, ValueError, KeyError, TypeError) as exc:
                self.errors["refresh"] = type(exc).__name__
            await asyncio.sleep(5)

    async def next_close(self):
        event = await self.closes.get()
        if isinstance(event, Exception):
            raise event
        return event

    def view(self, end: int) -> dict:
        book, previous = self.book.at(end)
        public, market = self.healthy("public", end), self.healthy("market", end)
        book_fresh = bool(
            public and book and 0 <= end - book["observed_at"] <= self.settings.realtime_stale_ms
        )
        tickers = [
            r
            for r in self.tickers
            if 0 <= end - r["observed_at"] <= self.settings.realtime_stale_ms
        ]
        marks = [r for r in self.marks if 0 <= end - r["time"] <= self.settings.realtime_stale_ms]
        return {
            "book": book if book_fresh else None,
            "previous_book": previous if book_fresh else None,
            "websocket_sequence_valid": self.book.valid and book_fresh,
            "trades": self.trades.at(
                end, connected=market and self.queue.empty() and not self._market_inflight
            ),
            "ticker": tickers[-1] if public and tickers else None,
            "mark": marks[-1] if market and marks else None,
            "liquidations": [
                r for r in self.liquidations.values() if end - 3_600_000 <= r["time"] <= end
            ],
            "liquidation_coverage_start": self.market_since if market else None,
            "errors": dict(self.errors),
        }
