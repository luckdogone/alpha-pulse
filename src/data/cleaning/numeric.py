import math

from data.errors import DataError


def number(value, *, positive: bool = False, nonnegative: bool = False) -> float:
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0) or (nonnegative and result < 0):
        raise DataError("non-finite or invalid numeric market value")
    return result
