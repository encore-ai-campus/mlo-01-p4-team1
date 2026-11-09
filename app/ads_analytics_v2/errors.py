class AnalyticsError(ValueError):
    """A safe error code; never include source payloads, URIs or credentials."""


class Conflict(AnalyticsError):
    pass


class CapacityExceeded(AnalyticsError):
    pass


class LeaseLost(AnalyticsError):
    pass
