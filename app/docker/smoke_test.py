"""Read-only HTTP smoke check for the local-only Docker Compose application."""
import argparse
import json
import sys
import urllib.error
import urllib.request


def fetch(label, url, required_key=None):
    with urllib.request.urlopen(url, timeout=8) as response:
        code = response.status
        content_type = response.headers.get("Content-Type", "")
        body = response.read()
    if code != 200:
        raise RuntimeError(f"{label} returned HTTP {code}")
    if required_key:
        if "json" not in content_type.lower():
            raise RuntimeError(f"{label} did not respond with JSON")
        data = json.loads(body)
        if required_key not in data:
            raise RuntimeError(f"{label} missing expected field {required_key}")
    print(f"PASS {label}: HTTP {code}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ads", action="store_true", help="also check ads API")
    args = parser.parse_args()
    cases = [
        ("Frontend", "http://127.0.0.1:5173/", None),
        ("Vite to Django auth", "http://127.0.0.1:5173/api/auth/me", "authenticated"),
        ("Django API", "http://127.0.0.1:8001/health/", "status"),
        ("Mailpit", "http://127.0.0.1:8025/", None),
    ]
    if args.ads:
        cases.append(("Ads Django", "http://127.0.0.1:8000/api/health", "service"))
    failures = []
    for label, url, required_key in cases:
        try:
            fetch(label, url, required_key)
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            print(f"FAIL {label}: {exc}", file=sys.stderr)
            failures.append(label)
    print(f"Result: {len(cases) - len(failures)}/{len(cases)} PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
