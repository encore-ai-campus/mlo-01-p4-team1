"""Validate candidate-tag envelopes without running inference or changing source data."""

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from .errors import ValidationError

SCHEMA_VERSION = "phase4-inference-v1"
MAX_RESULT_BYTES = 128 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RESULTS = 100
MAX_DURATION_MS = 2_147_483_647
REQUIRED_FIELDS = frozenset({"schema_version", "result_id", "batch_id", "task_id", "request_item_id",
                             "lease_token", "track_id", "source_ref", "model_version", "generated_at", "tags"})
OPTIONAL_FIELDS = frozenset({"payload_sha256", "metadata"})
HASH = re.compile(r"[0-9a-f]{64}\Z")
UTC_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)\Z")
GENRES = ("POP", "K-POP", "J-POP", "ROCK_PUNK_METAL", "ELECTRONIC_DANCE", "HIPHOP",
          "RNB_SOUL_FUNK", "JAZZ", "FOLK_COUNTRY_BLUES", "OPERA", "CLASSICAL", "NEWAGE",
          "REGGAE_SKA", "LATIN")
MOODS = ("CALM", "EMOTIONAL", "PEACEFUL", "DREAMY", "ENERGETIC", "BRIGHT", "PARTY",
         "GROOVY", "INTENSE")
TAG_CODES = {"genre": frozenset(GENRES), "mood": frozenset(MOODS)}


def canonical(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValidationError("INVALID_JSON_VALUE") from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def decode_json(data: bytes):
    try:
        def invalid_constant(_):
            raise ValidationError("INVALID_JSON_VALUE")
        return json.loads(data.decode("utf-8-sig"), object_pairs_hook=_pairs,
                          parse_constant=invalid_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValidationError("INVALID_JSON") from None


def _text(value, field, limit=128):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValidationError("INVALID_" + field.upper())


def _uuid(value, field):
    _text(value, field, 36)
    try:
        if str(UUID(value)) != value.lower():
            raise ValueError
    except ValueError:
        raise ValidationError("INVALID_" + field.upper()) from None


def validate_tags(tags):
    if not isinstance(tags, dict) or set(tags) != TAG_CODES.keys():
        raise ValidationError("INVALID_TAG_FIELDS")
    for kind, codes in tags.items():
        allowed = TAG_CODES[kind]
        if (not isinstance(codes, list) or len(codes) > len(allowed)
                or any(not isinstance(code, str) or code not in allowed for code in codes)):
            raise ValidationError("INVALID_TAG_CODES")
        if len(set(codes)) != len(codes):
            raise ValidationError("DUPLICATE_TAG_CODE")
    return tags


def validate_result(value: dict) -> dict:
    if not isinstance(value, dict) or not REQUIRED_FIELDS <= value.keys() <= REQUIRED_FIELDS | OPTIONAL_FIELDS:
        raise ValidationError("INVALID_RESULT_FIELDS")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValidationError("UNSUPPORTED_SCHEMA_VERSION")
    for field in ("result_id", "batch_id", "lease_token", "model_version"):
        _text(value[field], field)
    for field in ("task_id", "request_item_id"):
        _uuid(value[field], field)
    if value["track_id"] is not None:
        _text(value["track_id"], "track_id")
    _text(value["source_ref"], "source_ref", 500)
    stamp = value["generated_at"]
    if not isinstance(stamp, str) or not UTC_STAMP.fullmatch(stamp):
        raise ValidationError("INVALID_GENERATED_AT")
    try:
        parsed = datetime.fromisoformat(stamp)
        if parsed.utcoffset() != timedelta(0):
            raise ValueError
    except ValueError:
        raise ValidationError("INVALID_GENERATED_AT") from None
    validate_tags(value["tags"])
    if "metadata" in value:
        metadata = value["metadata"]
        if not isinstance(metadata, dict) or not metadata.keys() <= {"duration_ms", "album_name", "explicit"}:
            raise ValidationError("INVALID_METADATA")
        if "duration_ms" in metadata and (type(metadata["duration_ms"]) is not int
                                          or not 1 <= metadata["duration_ms"] <= MAX_DURATION_MS):
            raise ValidationError("INVALID_DURATION_MS")
        if "album_name" in metadata:
            _text(metadata["album_name"], "album_name", 300)
        if "explicit" in metadata and type(metadata["explicit"]) is not bool:
            raise ValidationError("INVALID_EXPLICIT")
    result = dict(value)
    expected = hashlib.sha256(canonical({key: item for key, item in value.items()
                                         if key != "payload_sha256"})).hexdigest()
    if "payload_sha256" in value:
        actual = value["payload_sha256"]
        if not isinstance(actual, str) or not HASH.fullmatch(actual) or actual != expected:
            raise ValidationError("PAYLOAD_HASH_MISMATCH")
    result["payload_sha256"] = expected
    body = canonical(result)
    if len(body) > MAX_RESULT_BYTES:
        raise ValidationError("RESULT_TOO_LARGE")
    # Return a detached value so mutating the input cannot change a validated envelope.
    return decode_json(body)


def validate_input(value) -> list[dict]:
    values = value if isinstance(value, list) else [value]
    if not 1 <= len(values) <= MAX_RESULTS:
        raise ValidationError("INVALID_RESULT_COUNT")
    if len(canonical(value)) > MAX_FILE_BYTES:
        raise ValidationError("INPUT_TOO_LARGE")
    return [validate_result(item) for item in values]


def load_results(path: str | Path) -> list[dict]:
    try:
        with Path(path).open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise ValidationError("INPUT_UNREADABLE") from None
    if len(data) > MAX_FILE_BYTES:
        raise ValidationError("INPUT_TOO_LARGE")
    return validate_input(decode_json(data))
