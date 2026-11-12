"""Bounded HTTP delivery. Real connections happen only on an explicit send call."""

import ipaddress
import math
import os
import re
import ssl
from dataclasses import dataclass
from urllib import error, parse, request

from .errors import TransportError, ValidationError

MAX_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes
    content_type: str = "application/json"


def validate_endpoint(endpoint: str, *, allow_loopback_http: bool = False) -> str:
    if not isinstance(endpoint, str) or len(endpoint) > 2048 or any(ord(c) < 33 for c in endpoint):
        raise ValidationError("INVALID_ENDPOINT")
    try:
        parts = parse.urlsplit(endpoint)
        port = parts.port
    except ValueError:
        raise ValidationError("INVALID_ENDPOINT") from None
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
            or parts.password or parts.fragment or parts.query or port == 0):
        raise ValidationError("INVALID_ENDPOINT")
    if parts.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            loopback = False
        if not allow_loopback_http or not loopback:
            raise ValidationError("HTTPS_REQUIRED")
    return endpoint


def token_from_environment(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
        raise ValidationError("INVALID_TOKEN_ENV")
    token = os.environ.get(name, "")
    if not token or len(token) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValidationError("TOKEN_UNAVAILABLE")
    return token


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPTransport:
    def __init__(self, endpoint: str, *, token_env: str = "PHASE4_SENDER_TOKEN",
                 allow_loopback_http: bool = False, timeout: float = 10,
                 max_response_bytes: int = MAX_RESPONSE_BYTES):
        self.endpoint = validate_endpoint(endpoint, allow_loopback_http=allow_loopback_http)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValidationError("INVALID_TIMEOUT")
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValidationError("INVALID_RESPONSE_LIMIT")
        self.token_env = token_env
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._opener = request.build_opener(
            request.ProxyHandler({}), request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect()
        )

    def check_ready(self):
        token_from_environment(self.token_env)

    def send(self, body: bytes, *, idempotency_key: str) -> HTTPResponse:
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[!-~]{1,128}", idempotency_key):
            raise ValidationError("INVALID_IDEMPOTENCY_KEY")
        return self._request(self.endpoint, "POST", body, idempotency_key=idempotency_key)

    def _request(self, endpoint: str, method: str, body: bytes | None = None,
                 *, idempotency_key: str | None = None) -> HTTPResponse:
        headers = {"Authorization": "Bearer " + token_from_environment(self.token_env),
                   "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        req = request.Request(endpoint, data=body, method=method, headers=headers)
        try:
            try:
                response = self._opener.open(req, timeout=self.timeout)
            except error.HTTPError as exc:
                response = exc
            with response:
                content_encoding = response.headers.get("Content-Encoding", "identity").lower()
                if content_encoding not in {"", "identity"}:
                    raise TransportError("UNSUPPORTED_RESPONSE_ENCODING")
                data = response.read(self.max_response_bytes + 1)
                if len(data) > self.max_response_bytes:
                    raise TransportError("RESPONSE_TOO_LARGE")
                return HTTPResponse(response.status, data, response.headers.get_content_type())
        except TransportError:
            raise
        except (TimeoutError, error.URLError, OSError, ValueError):
            raise TransportError("DELIVERY_UNAVAILABLE") from None
