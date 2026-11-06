"""Create private, random, local-only Compose credentials without printing secrets."""
from pathlib import Path
import os
import secrets
import sys
import base64
import stat

root = Path(__file__).resolve().parents[1]
target = root / ".env"

existing = ''
existed = target.exists() or target.is_symlink()
if existed:
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise SystemExit('Local .env must be a regular owner-only file.')
    existing = target.read_text()

keys = [
    "MLO_DEV_PG_PASSWORD",
    "MLO_DEV_API_SECRET_KEY",
    "MLO_DEV_ADS_SECRET_KEY",
    "MLO_DEV_ADS_INTERNAL_SECRET",
    "MLO_DEV_ADS_SIGNING_KEY",
]
generated = {key:secrets.token_urlsafe(48) for key in keys}
generated.update({key:base64.b64encode(secrets.token_bytes(32)).decode() for key in
    ('MLO_DEV_MEMBER_EMAIL_KEY','MLO_DEV_MEMBER_LOOKUP_KEY')})
present = {line.partition('=')[0].strip() for line in existing.splitlines() if '=' in line}
missing = {key:value for key,value in generated.items() if key not in present}
if not missing:
    print('Local .env already contains the required keys; leaving it untouched.')
    sys.exit(0)
lines = [
    "# GENERATED LOCAL-ONLY SECRETS. Never commit or send this file.",
    *[f"{key}={value}" for key,value in missing.items()],
    "",
]
fd = os.open(target, os.O_WRONLY | os.O_APPEND if existed else os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    stream.write(('\n' if existing and not existing.endswith('\n') else '')+'\n'.join(lines))
print(f"Prepared {target.name} with mode 0600; existing values were preserved and secrets are not printed.")
