from data.cleaning.numeric import number
from data.errors import DataError


def clean_trades(raw: list[dict]) -> list[dict]:
    if not raw:
        raise DataError("empty aggregate trade sample")
    rows = []
    for row in raw:
        if not isinstance(row["m"], bool):
            raise DataError("invalid buyer-maker flag")
        rows.append(
            {
                "price": number(row["p"], positive=True),
                "quantity": number(row["q"], nonnegative=True),
                "timestamp": row["T"],
                "buyer_maker": row["m"],
            }
        )
    return rows
