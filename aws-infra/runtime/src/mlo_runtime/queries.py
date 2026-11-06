"""Shared fixed Driver/block-manager ports; safe to import on bootstrap hosts."""

QUERIES = {
    "weather-stream": (19000, 19001),
    "feedback-postgresql": (19002, 19003),
    "feedback-analytics-batch": (19004, 19005),
}
