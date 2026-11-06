"""Loopback-only S3 protocol fixture for real SDK conditional/versioned operations."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.parse


class LocalS3:
    def __init__(self):
        self.lock = threading.RLock()
        self.objects, self.history, self.requests = {}, {}, []
        self.unavailable = False
        self.drop_after_put = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body=b"", **headers):
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def error(self, code, status):
                self.reply(
                    status,
                    f"<Error><Code>{code}</Code><Message>local fixture</Message></Error>".encode(),
                    **{"Content-Type": "application/xml"},
                )

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                key = urllib.parse.unquote(parsed.path)
                with fixture.lock:
                    fixture.requests.append(("GET", key))
                    if fixture.unavailable:
                        return self.error("ServiceUnavailable", 503)
                    versions = urllib.parse.parse_qs(parsed.query).get("versionId")
                    value = (
                        fixture.history.get((key, versions[0]))
                        if versions
                        else fixture.objects.get(key)
                    )
                    if value is None:
                        return self.error("NoSuchKey", 404)
                    raw, etag, version = value
                    self.reply(
                        200,
                        raw,
                        ETag=etag,
                        **{
                            "x-amz-version-id": version,
                            "Content-Type": "application/octet-stream",
                        },
                    )

            def do_PUT(self):
                parsed = urllib.parse.urlparse(self.path)
                key = urllib.parse.unquote(parsed.path)
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with fixture.lock:
                    fixture.requests.append(("PUT", key))
                    if fixture.unavailable:
                        return self.error("ServiceUnavailable", 503)
                    previous = fixture.objects.get(key)
                    if (
                        self.headers.get("If-None-Match") == "*"
                        and previous is not None
                    ):
                        return self.error("PreconditionFailed", 412)
                    if self.headers.get("If-Match") is not None and (
                        previous is None or previous[1] != self.headers["If-Match"]
                    ):
                        return self.error("PreconditionFailed", 412)
                    version = str(len(fixture.history) + 1)
                    etag = (
                        '"' + hashlib.sha256(raw + version.encode()).hexdigest() + '"'
                    )
                    value = raw, etag, version
                    fixture.objects[key] = value
                    fixture.history[key, version] = value
                    if fixture.drop_after_put:
                        fixture.drop_after_put = False
                        self.close_connection = True
                        return
                    self.reply(200, ETag=etag, **{"x-amz-version-id": version})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    @property
    def endpoint(self):
        return "http://127.0.0.1:" + str(self.server.server_port)

    def client(self):
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            region_name="ap-northeast-2",
            aws_access_key_id="local-only-key",
            aws_secret_access_key="local-only-secret",
            config=Config(
                connect_timeout=1,
                read_timeout=1,
                proxies={},
                retries={"total_max_attempts": 1, "mode": "standard"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                s3={"addressing_style": "path"},
                signature_version="s3v4",
            ),
        )
