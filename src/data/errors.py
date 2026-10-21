class DataError(RuntimeError):
    """Sanitized external-data failure; never contains credentials or response bodies."""


class DataUnavailable(DataError):
    """A data source requires configuration or is not available for this symbol."""


class SequenceError(DataError):
    """The local order book must be rebuilt before it can be used."""
