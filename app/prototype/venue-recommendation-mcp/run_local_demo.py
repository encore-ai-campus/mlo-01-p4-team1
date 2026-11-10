"""Start the venue MCP demo from a project checkout with an isolated SQLite database."""

import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import sys


MCP = Path(__file__).resolve().parent
APP = MCP.parents[1]


def key():
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-python", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18748)
    parser.add_argument("--kma-cache", type=Path, help="Optional existing KMA forecast cache")
    args = parser.parse_args()
    mcp_python = args.mcp_python.resolve(strict=True)
    if not mcp_python.is_file() or not 1024 <= args.port <= 65535:
        parser.error("Check the MCP Python executable and local port.")
    if not (APP / "frontend/dist/index.html").is_file():
        parser.error("Build app/frontend before starting the demo.")

    demo_dir = APP / ".local" / "venue-mcp"
    demo_dir.mkdir(parents=True, exist_ok=True)
    database = (demo_dir / "preview.sqlite3").resolve()
    base = f"http://127.0.0.1:{args.port}"
    os.environ.update({
        "DJANGO_SETTINGS_MODULE": "backend.cafe.settings",
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
        "DJANGO_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "DJANGO_CSRF_TRUSTED_ORIGINS": "",
        "C_LOCAL_DEMO": "1",
        "C_LOCAL_DATABASE": str(database),
        "INTEGRATION_ENABLED": "0",
        "PLATFORM_LOCAL_MVP": "0",
        "FRONTEND_REQUIRED": "1",
        "ADMIN_SSO_ENABLED": "0",
        "MEMBER_PUBLIC_ORIGIN": base,
        "MEMBER_LOOKUP_KEY": key(),
        "MEMBER_EMAIL_KEYS": json.dumps({"preview": key()}),
        "MEMBER_ACTIVE_EMAIL_KEY": "preview",
        "WEATHER_PROVIDER": "backend.weather.live.obtain",
        "VENUE_MCP_SERVER": str(MCP / "venue_recommendation_mcp.py"),
        "VENUE_MCP_PYTHON": str(mcp_python),
        "VENUE_MCP_CATALOG": str(MCP / "fixtures/csv_sample_catalog.json"),
        "VENUE_MCP_LOCATIONS": str(MCP / "fixtures/csv_sample_locations.json"),
        "VENUE_MCP_VENUES_CSV": str(APP / "backend/venues/sample_data/seoul_venues_demo_sample.csv"),
        "VENUE_WEATHER_BASE_URL": base,
    })
    if args.kma_cache:
        os.environ["KMA_LOCAL_CACHE_PATH"] = str(args.kma_cache.resolve(strict=True))

    os.chdir(APP)
    sys.path.insert(0, str(APP))
    from backend.cafe.asgi import application
    from django.conf import settings
    from django.core.management import call_command
    import uvicorn

    db = settings.DATABASES["default"]
    if db["ENGINE"] != "django.db.backends.sqlite3" or Path(db["NAME"]).resolve() != database:
        raise RuntimeError("The venue demo requires its isolated SQLite database.")
    call_command("check")
    call_command("migrate", interactive=False, verbosity=0)
    print(f"Venue demo: {base}/venues", flush=True)
    uvicorn.run(application, host="127.0.0.1", port=args.port, lifespan="on")


if __name__ == "__main__":
    main()
