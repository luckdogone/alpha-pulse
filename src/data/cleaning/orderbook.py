from data.cleaning.numeric import number
from data.errors import DataError


def clean_order_book(raw: dict) -> dict:
    bids = sorted(
        [[number(p, positive=True), number(q, nonnegative=True)] for p, q in raw["bids"]],
        reverse=True,
    )
    asks = sorted([[number(p, positive=True), number(q, nonnegative=True)] for p, q in raw["asks"]])
    bids, asks = [r for r in bids if r[1] > 0], [r for r in asks if r[1] > 0]
    if not bids or not asks or bids[0][0] >= asks[0][0] or bids[0][0] <= 0:
        raise DataError("invalid or crossed order book")
    return {
        "observed_at": raw.get("E", raw.get("T")),
        "last_update_id": raw["lastUpdateId"],
        "bids": bids,
        "asks": asks,
    }
