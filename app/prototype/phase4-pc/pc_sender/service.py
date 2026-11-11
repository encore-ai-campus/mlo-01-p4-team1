"""Deliver independently per result and require a matching reception ACK."""

import time

from .errors import SenderError, TransportError, ValidationError
from .store import LeaseLost
from .transport import MAX_RESPONSE_BYTES
from .validation import decode_json

ACK_FIELDS = {"result_id", "payload_sha256", "receipt_id", "status"}
SAFE_TRANSPORT_ERRORS = {"INVALID_ACK", "INVALID_HTTP_RESPONSE", "RESPONSE_TOO_LARGE",
                         "UNSUPPORTED_RESPONSE_ENCODING", "DELIVERY_UNAVAILABLE",
                         "TOKEN_UNAVAILABLE", "INVALID_IDEMPOTENCY_KEY", "INVALID_TOKEN_ENV"}


def receipt_from_ack(response, claim) -> str:
    if (not isinstance(response.body, bytes) or len(response.body) > MAX_RESPONSE_BYTES
            or response.content_type.lower().split(";", 1)[0].strip() != "application/json"):
        raise TransportError("INVALID_ACK")
    try:
        ack = decode_json(response.body)
    except ValidationError:
        raise TransportError("INVALID_ACK") from None
    if (not isinstance(ack, dict) or set(ack) != ACK_FIELDS
            or ack["result_id"] != claim.result_id
            or ack["payload_sha256"] != claim.payload_sha256
            or ack["status"] != "RECEIVED_PENDING_REVIEW"
            or not isinstance(ack["receipt_id"], str) or not ack["receipt_id"].strip()
            or len(ack["receipt_id"]) > 128
            or any(ord(c) < 32 or ord(c) == 127 for c in ack["receipt_id"])):
        raise TransportError("INVALID_ACK")
    return ack["receipt_id"]


def send_once(outbox, transport, *, clock=time.time, limit=100, lease_seconds=180,
              max_attempts=8) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValidationError("INVALID_SEND_LIMIT")
    # Configuration errors must not consume queued results or retry attempts.
    if hasattr(transport, "check_ready"):
        transport.check_ready()
    counts = {"claimed": 0, "received": 0, "retry": 0, "rejected": 0, "dead": 0, "lease_lost": 0}
    for _ in range(limit):
        claim = outbox.claim(now=clock(), lease_seconds=lease_seconds, max_attempts=max_attempts)
        if claim is None:
            break
        counts["claimed"] += 1
        receipt, retry, error_code = None, False, None
        try:
            response = transport.send(claim.body, idempotency_key=claim.result_id)
            if isinstance(response.status, bool) or not isinstance(response.status, int):
                raise TransportError("INVALID_HTTP_RESPONSE")
            if response.status in {200, 201}:
                receipt = receipt_from_ack(response, claim)
            elif response.status == 429:
                retry, error_code = True, "SERVER_RATE_LIMITED"
            elif 500 <= response.status <= 599:
                retry, error_code = True, "SERVER_UNAVAILABLE"
            elif response.status == 409:
                error_code = "SERVER_CONFLICT"
            elif response.status in {401, 403}:
                error_code = "SERVER_AUTH_REJECTED"
            elif response.status == 422:
                error_code = "SERVER_VALIDATION_REJECTED"
            elif 300 <= response.status <= 399:
                error_code = "REDIRECT_REJECTED"
            else:
                error_code = "HTTP_STATUS_REJECTED"
        except SenderError as exc:
            retry = True
            error_code = exc.code if exc.code in SAFE_TRANSPORT_ERRORS else "TRANSPORT_FAILURE"
        except (OSError, TimeoutError):
            retry, error_code = True, "DELIVERY_UNAVAILABLE"
        except Exception:  # noqa: BLE001 - isolate custom transport errors without leaking secrets.
            # Injected/custom transports must not leak arbitrary exception text or tokens.
            retry, error_code = True, "TRANSPORT_FAILURE"
        try:
            outbox.finish(claim, now=clock(), receipt_id=receipt, error_code=error_code,
                          retry=retry, max_attempts=max_attempts)
        except LeaseLost:
            counts["lease_lost"] += 1
            continue
        if receipt is not None:
            counts["received"] += 1
        elif retry:
            counts["dead" if claim.attempt >= max_attempts else "retry"] += 1
        else:
            counts["rejected"] += 1
    return counts
