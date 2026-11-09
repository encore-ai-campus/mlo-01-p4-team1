"""Read-only local demonstration API; not a replacement for common ADMIN auth."""
import hmac
import json
from urllib.parse import parse_qs

from .errors import AnalyticsError, CapacityExceeded, Conflict
from .reporting import report


def application(store, *, token):
    if not isinstance(token, str) or len(token) < 16:
        raise AnalyticsError("LOCAL_API_TOKEN_REQUIRED")

    def app(environ, start_response):
        def respond(status, body):
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                                    ("Content-Length", str(len(payload))), ("Cache-Control", "no-store")])
            return [payload]
        if environ.get("REQUEST_METHOD") != "GET":
            return respond("405 Method Not Allowed", {"code": "READ_ONLY_API"})
        if environ.get("PATH_INFO") == "/health":
            return respond("200 OK", {"process_alive": True, "external_services_verified": False})
        supplied = environ.get("HTTP_AUTHORIZATION", "")
        if not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + token).encode("utf-8")):
            return respond("401 Unauthorized", {"code": "AUTH_REQUIRED"})
        if environ.get("PATH_INFO") != "/reports":
            return respond("404 Not Found", {"code": "NOT_FOUND"})
        try:
            query = environ.get("QUERY_STRING", "")
            if len(query) > 4096:
                raise AnalyticsError("QUERY_TOO_LONG")
            values = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
            if "cutoff" not in values or any(len(value) != 1 or not value[0] for value in values.values()):
                raise AnalyticsError("INVALID_QUERY")
            allowed = {"cutoff", "settled_through", "date_from", "date_to", "ad_id", "placement", "media_id"}
            if set(values) - allowed:
                raise AnalyticsError("UNKNOWN_QUERY_PARAMETER")
            result = report(store.read_events(), **{name: value[0] for name, value in values.items()})
            return respond("200 OK", result)
        except CapacityExceeded as error:
            return respond("503 Service Unavailable", {"code": str(error)})
        except Conflict as error:
            return respond("409 Conflict", {"code": str(error)})
        except AnalyticsError as error:
            return respond("400 Bad Request", {"code": str(error)})
        except (ValueError, TypeError):
            return respond("400 Bad Request", {"code": "INVALID_QUERY"})
        except Exception:
            return respond("503 Service Unavailable", {"code": "REPORT_UNAVAILABLE"})
    return app
