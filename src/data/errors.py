class DataError(RuntimeError):
    """Sanitized external-data failure; never contains credentials or response bodies."""
