"""Shared fixed Driver/block-manager ports; safe to import on bootstrap hosts."""

QUERIES = {
    "weather-stream": (19000, 19001),
    "feedback-postgresql": (19002, 19003),
    "feedback-analytics-batch": (19004, 19005),
}

# Never derive executor ports from registry order: 19006~19008 already belong
# to the three legacy applications. Track2 is dormant until an artifact is accepted.
EXECUTOR_PORTS = dict(zip(QUERIES, (19006, 19007, 19008)))
TRACK2_QUERIES = {"venues-catalog": (19010, 19011)}
TRACK2_EXECUTOR_PORTS = {"venues-catalog": 19012}


def validate_ports(queries=None, executors=None):
    from .operations import UnsafeTransition

    queries = {**QUERIES, **TRACK2_QUERIES} if queries is None else queries
    executors = (
        {**EXECUTOR_PORTS, **TRACK2_EXECUTOR_PORTS} if executors is None else executors
    )
    if set(queries) != set(executors):
        raise UnsafeTransition("query/executor registry mismatch")
    ports = [p for pair in queries.values() for p in pair] + list(executors.values())
    if (
        any(type(p) is not int or not 19000 <= p <= 19099 for p in ports)
        or len(ports) != len(set(ports))
        or any(len(pair) != 2 for pair in queries.values())
    ):
        raise UnsafeTransition("Driver/block/executor port collision or invalid pool")


validate_ports()
