"""Normalize exchange candles and make gaps explicit; never synthesize missing rows."""

from typing import Any

from domain.models import Candle


def from_rest(row: list[Any], at: int) -> "Candle":
    return Candle(
        open_time=row[0],
        open=row[1],
        high=row[2],
        low=row[3],
        close=row[4],
        volume=row[5],
        close_time=row[6],
        quote_volume=row[7],
        trades=row[8],
        taker_buy_volume=row[9],
        taker_buy_quote_volume=row[10] if len(row) > 10 else None,
        closed=row[6] < at,
    )


def from_ws(row: dict[str, Any]) -> "Candle":
    return Candle(
        open_time=row["t"],
        close_time=row["T"],
        open=row["o"],
        high=row["h"],
        low=row["l"],
        close=row["c"],
        volume=row["v"],
        quote_volume=row["q"],
        trades=row["n"],
        taker_buy_volume=row["V"],
        taker_buy_quote_volume=row.get("Q"),
        closed=row["x"],
    )


def merge_page(
    by_time: dict[int, Candle], page: list[Candle], start_ms: int | None, end_ms: int
) -> None:
    """Keep the last observation for each open time within the requested range."""
    for candle in page:
        if (start_ms is None or candle.open_time >= start_ms) and candle.open_time <= end_ms:
            by_time[candle.open_time] = candle


def ordered_candles(by_time: dict[int, Candle]) -> list[Candle]:
    return sorted(by_time.values(), key=lambda c: c.open_time)


def find_gaps(rows: list[Candle]) -> list[int]:
    return [
        b.open_time for a, b in zip(rows, rows[1:], strict=False) if b.open_time != a.close_time + 1
    ]
