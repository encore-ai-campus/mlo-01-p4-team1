"""Local acceptance double only. This is not a business app or home model."""

import argparse
import http.server
import json
from pathlib import Path
import time
import urllib.request


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, value):
        encoded = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path.startswith("http://"):
            if self.headers.get("Proxy-Authorization") != "Bearer smoke":
                return self.reply(407, {"error": "proxy auth"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(self.path, timeout=2) as upstream:
                return self.reply(
                    200,
                    {
                        "proxy": self.server.identity,
                        "upstream": json.loads(upstream.read()),
                    },
                )
        revision = (
            Path(self.server.revision).read_text() if self.server.revision else "smoke"
        )
        self.reply(
            503 if revision == "bad" else 200,
            {"revision": revision, "identity": self.server.identity},
        )

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer smoke":
            return self.reply(401, {"error": "auth"})
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.server.capture:
            with open(self.server.capture, "a") as stream:
                stream.write(body.decode() + "\n")
        value = json.loads(body)
        if self.server.delay:
            time.sleep(self.server.delay)
        self.reply(
            self.server.status,
            {
                "request_id": value["request_id"],
                "revision": value["revision"],
                "candidate": value["candidates"][0],
            },
        )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--revision")
    p.add_argument("--identity", default="smoke")
    p.add_argument("--capture")
    p.add_argument("--status", type=int, default=200)
    p.add_argument("--delay", type=float, default=0)
    args = p.parse_args()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    for name in ["revision", "identity", "capture", "status", "delay"]:
        setattr(server, name, getattr(args, name))
    print(server.server_port, flush=True)
    server.serve_forever()
