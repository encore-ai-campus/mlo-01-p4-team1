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
RESEARCH_CATALOG = MCP / "fixtures" / "research_preview.json"


def key():
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def demo_catalog(public_catalog, use_research, state_dir):
    """Select a local snapshot without mixing the demo databases."""
    if public_catalog is not None and use_research:
        raise ValueError("Choose either --public-catalog or --research-preview, not both.")
    if use_research:
        public_catalog = RESEARCH_CATALOG
        state_dir = state_dir or APP / ".local" / "venue-research-demo"
    if public_catalog is None:
        return None, (state_dir or APP / ".local" / "venue-mcp").resolve()
    if state_dir is None:
        raise ValueError("A public catalog requires a separate --state-dir.")
    catalog = Path(public_catalog).resolve()
    if not catalog.is_file():
        raise ValueError("The requested local catalog is missing.")
    state = Path(state_dir).resolve()
    if state == (APP / ".local" / "venue-mcp").resolve():
        raise ValueError("Choose a state directory separate from the standard demo.")
    return catalog, state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-python", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18748)
    parser.add_argument("--kma-cache", type=Path, help="Optional existing KMA forecast cache")
    parser.add_argument("--public-catalog", type=Path, help="Published venue/menu snapshot for this local demo")
    parser.add_argument("--research-preview", action="store_true", help="Use the existing local-only research snapshot")
    parser.add_argument("--state-dir", type=Path, help="Directory for the local demo SQLite database")
    parser.add_argument("--frontend-dist", type=Path, default=APP / "frontend/dist", help="Built frontend directory")
    parser.add_argument("--llm-provider", choices=("codex_cli", "gemini"), help="Explicitly enable the local chat provider")
    parser.add_argument("--codex-cli-executable", type=Path, help="Optional path to the trusted Codex CLI executable")
    args = parser.parse_args()
    if args.codex_cli_executable is not None:
        if args.llm_provider != "codex_cli" or not args.codex_cli_executable.is_file():
            parser.error("A Codex CLI executable requires --llm-provider codex_cli and an existing file.")
        os.environ["VENUE_CODEX_CLI"] = str(args.codex_cli_executable.resolve(strict=True))
    mcp_python = args.mcp_python.resolve(strict=True)
    if not mcp_python.is_file() or not 1024 <= args.port <= 65535:
        parser.error("Check the MCP Python executable and local port.")
    frontend_dist = args.frontend_dist.resolve()
    if not (frontend_dist / "index.html").is_file():
        parser.error("Build app/frontend before starting the demo.")
    try:
        public_catalog, demo_dir = demo_catalog(args.public_catalog, args.research_preview, args.state_dir)
    except ValueError as exc:
        parser.error(str(exc))
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
        "VENUE_LLM_PROVIDER": args.llm_provider or "",
        "VENUE_MCP_SERVER": str(MCP / "venue_recommendation_mcp.py"),
        "VENUE_MCP_PYTHON": str(mcp_python),
        "VENUE_MCP_CATALOG": str(MCP / "fixtures/csv_sample_catalog.json"),
        "VENUE_MCP_LOCATIONS": str(MCP / "fixtures/csv_sample_locations.json"),
        "VENUE_MCP_VENUES_CSV": str(APP / "backend/venues/sample_data/seoul_venues_demo_sample.csv"),
        "VENUE_WEATHER_BASE_URL": base if public_catalog is None or args.kma_cache else "",
    })
    if public_catalog is not None:
        os.environ["VENUE_MCP_PUBLIC_CATALOG"] = str(public_catalog)
    else:
        os.environ.pop("VENUE_MCP_PUBLIC_CATALOG", None)
    if args.kma_cache:
        os.environ["KMA_LOCAL_CACHE_PATH"] = str(args.kma_cache.resolve(strict=True))

    os.chdir(APP)
    sys.path.insert(0, str(APP))
    from django.conf import settings
    from django.core.management import call_command
    import uvicorn

    settings.FRONTEND_DIST = frontend_dist
    from backend.cafe.asgi import application

    db = settings.DATABASES["default"]
    if db["ENGINE"] != "django.db.backends.sqlite3" or Path(db["NAME"]).resolve() != database:
        raise RuntimeError("The venue demo requires its isolated SQLite database.")
    call_command("check")
    call_command("migrate", interactive=False, verbosity=0)
    print(f"Venue demo: {base}/venues", flush=True)
    uvicorn.run(application, host="127.0.0.1", port=args.port, lifespan="on")


if __name__ == "__main__":
    main()
