import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .errors import AnalyticsError

EVENT_SCHEMA = "ads-analytics-event-v2.1"
EVENT_TYPES = {"VIEWABLE_IMPRESSION", "CLICK"}
RESULTS = {
    "COUNTED", "DELIVERY_DUPLICATE", "REPEAT_SUPPRESSED", "LATE_EVENT",
    "DELIVERY_INVALID", "MEASUREMENT_UNAVAILABLE",
}


def timestamp(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        raise AnalyticsError("INVALID_TIMESTAMP") from None


def stamp(value):
    return timestamp(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise AnalyticsError("INVALID_IDENTIFIER")
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise AnalyticsError("INVALID_IDENTIFIER")
    return value


@dataclass(frozen=True)
class Event:
    event_id: str
    delivery_id: str
    ad_id: str
    creative_version: str
    placement: str
    media_id: str
    event_type: str
    occurred_at: datetime
    received_at: datetime
    result: str
    counted: bool
    is_test: bool
    source_policy_version: str
    event_schema_version: str = EVENT_SCHEMA

    def __post_init__(self):
        for field in (
            "event_id", "delivery_id", "ad_id", "creative_version", "media_id",
            "source_policy_version",
        ):
            identifier(getattr(self, field))
        if self.event_schema_version != EVENT_SCHEMA:
            raise AnalyticsError("UNSUPPORTED_EVENT_SCHEMA")
        if self.placement not in ("top", "bottom") or self.event_type not in EVENT_TYPES:
            raise AnalyticsError("INVALID_EVENT_KIND")
        if self.result not in RESULTS or type(self.counted) is not bool or type(self.is_test) is not bool:
            raise AnalyticsError("INVALID_DECISION")
        if self.counted != (self.result == "COUNTED") or (self.is_test and self.counted):
            raise AnalyticsError("INCONSISTENT_SOURCE_DECISION")
        object.__setattr__(self, "occurred_at", timestamp(self.occurred_at))
        object.__setattr__(self, "received_at", timestamp(self.received_at))
        if self.counted and not timedelta(0) <= self.received_at - self.occurred_at <= timedelta(seconds=300):
            raise AnalyticsError("INVALID_COUNTED_EVENT_TIME")

    def envelope(self):
        data = dict(self.__dict__)
        data.update(occurred_at=stamp(self.occurred_at), received_at=stamp(self.received_at))
        return data

    @property
    def fingerprint(self):
        return digest(self.envelope())

    @classmethod
    def from_envelope(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise AnalyticsError("INVALID_EVENT_FIELDS")
        return cls(**value)

    @classmethod
    def from_v1_record(cls, value, *, media_id):
        """Whitelist an authorized V1 snapshot; do not export cookies, tokens or guest IDs."""
        if not isinstance(value, dict):
            raise AnalyticsError("INVALID_SOURCE_RECORD")
        names = (
            "event_id", "delivery_id", "ad_id", "creative_version", "placement",
            "event_type", "occurred_at", "received_at", "result", "counted", "is_test",
        )
        if not set(names) <= value.keys() or "ads_policy_version" not in value:
            raise AnalyticsError("MISSING_SOURCE_FIELDS")
        return cls(**{name: value[name] for name in names}, media_id=media_id,
                   source_policy_version=value["ads_policy_version"])
