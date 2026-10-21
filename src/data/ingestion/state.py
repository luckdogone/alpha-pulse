"""Bounded live state; transport disconnects invalidate continuity immediately."""

from collections import deque

from data.cleaning.numeric import number
from data.cleaning.orderbook import clean_order_book
from data.cleaning.trades import clean_trades
from data.errors import SequenceError


class OrderBookState:
    def __init__(self, levels: int = 100):
        self.levels = levels
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.update_id: int | None = None
        self.valid = False
        self.history: deque[dict] = deque(maxlen=128)
        self._known_bid_floor = self._known_ask_ceiling = 0.0
        self._required_levels = 0

    def invalidate(self):
        self.valid = False
        self.update_id = None
        self.history.clear()

    def seed(self, raw: dict):
        book = clean_order_book(raw)
        self.bids, self.asks = dict(book["bids"]), dict(book["asks"])
        self._known_bid_floor, self._known_ask_ceiling = min(self.bids), max(self.asks)
        self._required_levels = min(self.levels, len(self.bids), len(self.asks))
        self.update_id = int(book["last_update_id"])
        self.valid = False
        self.history.clear()

    def apply(self, event: dict) -> bool:
        if self.update_id is None:
            raise SequenceError("order book needs a REST snapshot")
        first, final, previous = int(event["U"]), int(event["u"]), int(event["pu"])
        if final < self.update_id or (self.valid and final == self.update_id):
            return False
        if (
            first > final
            or (self.valid and previous != self.update_id)
            or (not self.valid and not first <= self.update_id <= final)
        ):
            self.invalidate()
            raise SequenceError("order book update sequence is discontinuous")
        bids, asks = self.bids.copy(), self.asks.copy()
        for side, updates in ((bids, event["b"]), (asks, event["a"])):
            for p, q in updates:
                price, size = number(p, positive=True), number(q, nonnegative=True)
                if size:
                    side[price] = size
                else:
                    side.pop(price, None)
        if not bids or not asks or max(bids) >= min(asks) or len(bids) + len(asks) > 20_000:
            self.invalidate()
            raise SequenceError("local order book invalid or exceeds retained depth capacity")
        if (
            sum(p >= self._known_bid_floor for p in bids) < self._required_levels
            or sum(p <= self._known_ask_ceiling for p in asks) < self._required_levels
        ):
            self.invalidate()
            raise SequenceError("order book reserve depleted; unseen levels require a new snapshot")
        # Retain reserve levels internally; trimming every update would lose newly exposed levels.
        self.bids, self.asks, self.update_id, self.valid = bids, asks, final, True
        self.history.append(
            {
                "observed_at": int(event["E"]),
                "event_time": int(event["E"]),
                "transaction_time": int(event.get("T", event["E"])),
                "last_update_id": final,
                "bids": [list(r) for r in sorted(bids.items(), reverse=True)[: self.levels]],
                "asks": [list(r) for r in sorted(asks.items())[: self.levels]],
            }
        )
        return True

    def at(self, end: int) -> tuple[dict | None, dict | None]:
        rows = [r for r in self.history if r["observed_at"] <= end] if self.valid else []
        return (rows[-1] if rows else None, rows[-2] if len(rows) > 1 else None)


class TradeWindow:
    def __init__(self, minutes: int = 5):
        self.duration_ms = minutes * 60_000
        self.rows: dict[int, dict] = {}
        self.start: int | None = None
        self.complete = False
        self.last_id: int | None = None

    def invalidate(self):
        self.complete = False

    def seed(self, data: dict):
        # REST can finish after new WS trades arrive; merge by ID instead of replacing live rows.
        if data["trades"]:
            clean_trades(data["trades"])
        for row in data["trades"]:
            self.rows[int(row["a"])] = row
        self.start = data["window_start"]
        self.complete = data["complete"]
        self.last_id = max(self.rows, default=None)
        self.prune(data["window_end"])
        ids = sorted(self.rows)
        self.complete = self.complete and all(
            b == a + 1 for a, b in zip(ids, ids[1:], strict=False)
        )

    def add(self, row: dict) -> bool:
        ident = int(row["a"])
        if ident in self.rows:
            return False
        # Queued events older than the REST seed have already been covered, or pruned.
        if self.last_id is not None and ident < self.last_id:
            return False
        gap = self.last_id is not None and ident != self.last_id + 1
        if gap:
            self.complete = False
        self.rows[ident] = row
        self.last_id = max(ident, self.last_id or ident)
        self.prune(int(row["T"]))
        return gap

    def prune(self, end: int):
        cutoff = end - self.duration_ms
        self.rows = {i: r for i, r in self.rows.items() if int(r["T"]) >= cutoff}
        if len(self.rows) > 200_000:
            self.complete = False
            self.rows = dict(sorted(self.rows.items())[-200_000:])

    def at(self, end: int, *, connected: bool) -> dict:
        start = end - self.duration_ms
        rows = [r for _, r in sorted(self.rows.items()) if start <= int(r["T"]) <= end]
        return {
            "trades": rows,
            "window_start": max(start, self.start if self.start is not None else end),
            "window_end": end,
            "complete": self.complete
            and connected
            and self.start is not None
            and self.start <= start,
            "truncated": False,
            "id_gaps": [],
            "source": "binance_ws_with_rest_backfill",
            "observed_at": end,
        }
