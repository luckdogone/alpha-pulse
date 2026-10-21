from data.cleaning.numeric import number


def validate_derivative(name: str, data: dict | list) -> dict | list:
    """Validate required point metrics, preserving archived provider fields and units."""
    if name == "mark_price":
        number(data["lastFundingRate"])
        number(data["markPrice"], positive=True)
    elif name == "open_interest":
        number(data["openInterest"], nonnegative=True)
    return data
