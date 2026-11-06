"""Bounded authenticated TLS Gateway relay; home owns deduplication and slots."""

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .operations import UnsafeTransition


def envelope(raw):
    value = json.loads(raw)
    if set(value) != {"request_id", "deadline", "revision", "candidates", "payload"}:
        raise UnsafeTransition("invalid model envelope fields")
    if not isinstance(value["request_id"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value["request_id"]
    ):
        raise UnsafeTransition("invalid request ID")
    if not isinstance(value["deadline"], (int, float)) or not math.isfinite(
        value["deadline"]
    ):
        raise UnsafeTransition("invalid deadline")
    if (
        not isinstance(value["revision"], str)
        or not value["revision"]
        or not isinstance(value["candidates"], list)
        or not value["candidates"]
        or any(not isinstance(x, str) for x in value["candidates"])
    ):
        raise UnsafeTransition("invalid candidate/revision")
    if not isinstance(value["payload"], dict) or set(value["payload"]) - {
        "prompt",
        "context",
    }:
        raise UnsafeTransition("unsupported model payload fields")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def tls_opener(ca_file=None):
    context = ssl.create_default_context(cafile=ca_file)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        NoRedirect(),
    )


def protected_secret(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise UnsafeTransition("relay secret must be protected 0600")
    value = path.read_text().strip()
    if not value:
        raise UnsafeTransition("empty relay secret")
    return value


def relay_handler(
    home_url, control_token, home_token, ca_file, disconnect_after_submit=False
):
    parsed = urllib.parse.urlsplit(home_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise UnsafeTransition("home API must be a bound HTTPS origin")
    opener = tls_opener(ca_file)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Tokens and request bodies must not enter server access logs.

        def _handle(self):
            if not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + control_token
            ):
                self.send_error(401)
                return
            if not re.fullmatch(
                r"/(?:requests(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,127})?|status)", self.path
            ):
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 65536:
                    raise UnsafeTransition("model request too large")
                raw = self.rfile.read(length) if self.command == "POST" else None
                if self.command == "POST":
                    value = envelope(raw)
                    if (
                        self.path != "/requests"
                        or self.headers.get("X-Request-ID") != value["request_id"]
                    ):
                        raise UnsafeTransition("request ID header mismatch")
                    remaining = value["deadline"] - time.time()
                    if remaining <= 0:
                        self.send_error(408)
                        return
                else:
                    remaining = 5
                request = urllib.request.Request(
                    home_url.rstrip("/") + self.path,
                    data=raw,
                    method=self.command,
                    headers={
                        "Authorization": "Bearer " + home_token,
                        "Content-Type": "application/json",
                        "X-Request-ID": self.headers.get("X-Request-ID", ""),
                    },
                )
                try:
                    response = opener.open(request, timeout=min(remaining, 5))
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    body = response.read(1024 * 1024 + 1)
                    if len(body) > 1024 * 1024:
                        raise UnsafeTransition("home response too large")
                    status = response.status
                if disconnect_after_submit and self.command == "POST":
                    # Explicit lab fault injection: home has already accepted it.
                    self.close_connection = True
                    self.connection.close()
                    return
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ValueError, TypeError, UnsafeTransition):
                self.send_error(400)
            except (OSError, urllib.error.URLError):
                self.send_error(503)

        do_POST = do_GET = do_DELETE = _handle

    return Handler


def tls_server(handler, certificate, key, address=("127.0.0.1", 0)):
    server = ThreadingHTTPServer(address, handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def check_home(home_url, token, ca_file):
    if not home_url.startswith("https://"):
        raise UnsafeTransition("home health requires HTTPS")
    request = urllib.request.Request(
        home_url.rstrip("/") + "/status", headers={"Authorization": "Bearer " + token}
    )
    with tls_opener(ca_file).open(request, timeout=3) as response:
        value = json.loads(response.read(65536))
        if response.status != 200 or value.get("ready") is not True:
            raise UnsafeTransition("home API readiness unconfirmed")
    return {
        "ready": True,
        "scope": "authenticated home HTTPS status; GPU performance unconfirmed",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, default=Path("/etc/mlo/relay.json"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.binding.is_symlink() or args.binding.stat().st_mode & 0o077:
        raise UnsafeTransition("relay binding must be protected 0600")
    config = json.loads(args.binding.read_text())
    if config.get("ready") is not True or config.get("example_only") is not False:
        raise UnsafeTransition("relay inputs are not accepted for execution")
    if args.check:
        print(
            json.dumps(
                check_home(
                    config["home_url"],
                    protected_secret(config["home_token_file"]),
                    config["home_ca"],
                ),
                sort_keys=True,
            )
        )
        return
    handler = relay_handler(
        config["home_url"],
        protected_secret(config["control_token_file"]),
        protected_secret(config["home_token_file"]),
        config["home_ca"],
    )
    server = tls_server(
        handler,
        config["certificate"],
        config["key"],
        (config.get("listen_address", "127.0.0.1"), config.get("port", 8088)),
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
