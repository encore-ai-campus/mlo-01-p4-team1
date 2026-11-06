"""Create private, random, local-only Compose credentials without printing secrets."""
from pathlib import Path
import os
import secrets
import sys

root = Path(__file__).resolve().parents[1]
target = root / ".env"

if target.exists():
    print("Local .env already exists; leaving it untouched.")
    sys.exit(0)

keys = [
    "MLO_DEV_PG_PASSWORD",
    "MLO_DEV_API_SECRET_KEY",
    "MLO_DEV_ADS_SECRET_KEY",
    "MLO_DEV_ADS_INTERNAL_SECRET",
    "MLO_DEV_ADS_SIGNING_KEY",
]
lines = [
    "# GENERATED LOCAL-ONLY SECRETS. Never commit or send this file.",
    *[f"{key}={secrets.token_urlsafe(48)}" for key in keys],
    "",
]
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    stream.write("\n".join(lines))
print(f"Created {target.name} with mode 0600; values are intentionally not printed.")
