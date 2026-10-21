from data.catalog import METRICS
from data.cleaning.numeric import number
from data.errors import DataError

_FIELDS = {
    "mark_price": {
        "lastFundingRate": {},
        "markPrice": {"positive": True},
        "indexPrice": {"positive": True},
    },
    "open_interest": {"openInterest": {"nonnegative": True}},
    "open_interest_history": {
        "sumOpenInterest": {"nonnegative": True},
        "sumOpenInterestValue": {"nonnegative": True},
    },
    "open_interest_history_1h": {
        "sumOpenInterest": {"nonnegative": True},
        "sumOpenInterestValue": {"nonnegative": True},
    },
    "taker_buy_sell_ratio": {
        "buyVol": {"nonnegative": True},
        "sellVol": {"nonnegative": True},
        "buySellRatio": {"nonnegative": True},
    },
    "funding_history": {"fundingRate": {}, "markPrice": {"positive": True}},
    "funding_info": {
        "adjustedFundingRateCap": {},
        "adjustedFundingRateFloor": {},
        "fundingIntervalHours": {"positive": True},
    },
}


def validate_derivative(name: str, data: dict | list) -> dict | list:
    """Validate required fields without rewriting archived numeric strings or units."""
    spec = METRICS[name]
    fields = _FIELDS.get(
        name,
        {
            "longShortRatio": {"nonnegative": True},
            "longAccount": {"nonnegative": True},
            "shortAccount": {"nonnegative": True},
        },
    )
    is_series = spec.period is not None or name in {"funding_history", "funding_info"}
    if not isinstance(data, list if is_series else dict):
        raise DataError("invalid derivative response envelope")
    rows = data if is_series else [data]
    for row in rows:
        for field, constraints in fields.items():
            number(row[field], **constraints)
        if name != "funding_info":
            if int(row[spec.time_field]) < 0:
                raise DataError("invalid derivative timestamp")
        if name == "mark_price" and int(row["nextFundingTime"]) < 0:
            raise DataError("invalid next funding time")
    return data
