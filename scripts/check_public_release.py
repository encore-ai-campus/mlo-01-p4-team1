"""Public-source security gate: prevent accidental publication of private materials.

Usage:
    python3 scripts/check_public_release.py
    python3 scripts/check_public_release.py --staged

Only filenames and line numbers are displayed, never secret matches.
This does not replace manual review, a full secret scanning system or history audit.
"""
import argparse
from collections import defaultdict
from pathlib import Path
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_INFRA_DOCS = {
    "docs/handoff/infra/BASELINE.md",
    "docs/handoff/infra/OWNERSHIP.md",
    "docs/handoff/infra/TESTING.md",
}
BLOCKED_PREFIXES = (
    "docs/internal/",
    "app/docs/docker-migration/",
    "app/docs/internal/",
    "app/docs/acceptance/",
    "app/docs/development/",
    "app/docs/handoff/",
    "app/docs/runbook/",
    "app/docs/fixtures/",
    "app/analysis/",
    ".local/",
)
SENSITIVE_SUFFIXES = (
    ".pem", ".key", ".pfx", ".p12", ".tfstate", ".tfplan",
    ".sqlite", ".sqlite3", ".db", ".dump", ".bak",
)
PRIVACY_DOC_NAME = re.compile(
    r"(?:privacy[_-]?audit|security[_-]?audit|personal[_-]?data|"
    r"개인정보.{0,12}(?:검사|점검|감사|보고)|비밀정보.{0,12}(?:점검|보고))",
    re.IGNORECASE,
)
CREDENTIALS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    "aws_access_key": re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github_token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "openai_key": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{35,}\b"),
    "google_api_key": re.compile(rb"\bAIza[A-Za-z0-9_-]{35}\b"),
    "slack_token": re.compile(rb"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
    "discord_webhook": re.compile(rb"https?://(?:discord|discordapp)\.com/api/webhooks/[0-9]{15,}/[A-Za-z0-9_-]{30,}"),
    "jwt_token": re.compile(rb"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"),
    "korean_id_hyphenated": re.compile(rb"(?<!\d)\d{6}-[1-4]\d{6}(?!\d)"),
    "mobile_hyphenated": re.compile(rb"(?<!\d)01[016789]-\d{3,4}-\d{4}(?!\d)"),
    "private_owner_alias": re.compile(b"2ee" + b"logan", re.IGNORECASE),
}
# The string is intentionally split to avoid self-detection by scanning source.
PERSONAL_PATH = re.compile(
    b"/" + b"Users/" + rb"[A-Za-z0-9_.-]+/"
    + rb"|C:[/\\]Users[/\\][A-Za-z0-9_.-]+[/\\]"
)
EMAIL = re.compile(rb"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,12}\b")
BLOCKED_PNG_CHUNKS = {b"eXIf", b"iTXt", b"tEXt", b"zTXt", b"tIME"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".woff2", ".jar", ".ico", ".pdf"}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def candidate_paths(staged):
    if staged:
        paths = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    else:
        paths = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted({os.fsdecode(x) for x in paths.split(b"\0") if x})


def is_email_fixture(data, match):
    s = match.group().lower()
    if s.endswith((b"@example.com", b"@example.org", b"@example.net",
                   b"@users.noreply.github.com", b"@noreply.github.com",
                   b".test", b".invalid", b".example")):
        return True
    lo = data.rfind(b"\n", 0, match.start()) + 1
    hi = data.find(b"\n", match.end())
    line = data[lo:hi if hi >= 0 else None].lower()
    return (
        b"journalctl -u" in line
        or b"service@" in line
        or b"/ops/app/dashboards#" in line
        or b"https://user:" in line
        or b"http://user:" in line
    )


def png_privacy_chunks(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ["invalid_png_header"]
    pos = 8
    leaked = []
    while pos + 12 <= len(data):
        size = int.from_bytes(data[pos:pos + 4], "big")
        typ = data[pos + 4:pos + 8]
        if pos + size + 12 > len(data):
            return ["invalid_png_structure"]
        if typ in BLOCKED_PNG_CHUNKS:
            leaked.append(typ.decode("ascii"))
        pos += size + 12
        if typ == b"IEND":
            return leaked
    return ["invalid_png_end"]


def inspect(staged):
    critical = defaultdict(list)
    warnings = defaultdict(list)
    paths = candidate_paths(staged)
    if staged and not paths:
        print("FAIL: no staged files")
        return 2
    scanned = 0
    for name in paths:
        pp = Path(name)
        if staged:
            try:
                data = git("show", ":" + name)
            except subprocess.CalledProcessError:
                critical["unreadable_staged_path"].append((name, []))
                continue
        else:
            src = ROOT / name
            if not src.is_file():
                continue
            data = src.read_bytes()
        if any(name.startswith(p) for p in BLOCKED_PREFIXES):
            critical["private_directory"].append((name, []))
        if name.startswith("docs/handoff/infra/") and name not in PUBLIC_INFRA_DOCS:
            critical["private_infra_note"].append((name, []))
        if PRIVACY_DOC_NAME.search(name):
            critical["security_audit_document_name"].append((name, []))
        if pp.suffix.lower() in SENSITIVE_SUFFIXES or pp.name.startswith(".env") and pp.name != ".env.example":
            critical["sensitive_file_type"].append((name, []))
        if name.endswith("spotify_dataset.csv"):
            critical["restricted_music_dataset"].append((name, []))
        if len(data) > 30 * 1024 * 1024:
            critical["unscanned_oversize_file"].append((name, []))
            continue
        if pp.suffix.lower() == ".png":
            for kind in png_privacy_chunks(data):
                critical["private_image_metadata"].append((name, [kind]))
            continue
        if pp.suffix.lower() in BINARY_EXT or b"\0" in data[:4096]:
            warnings["binary_pixels_not_inspected"].append((name, []))
            continue
        scanned += 1
        for key, rule in CREDENTIALS.items():
            matches = list(rule.finditer(data))
            if matches:
                lines = sorted({data.count(b"\n", 0, m.start()) + 1 for m in matches})
                critical[key].append((name, lines[:8]))
        for key, rule in (("real_home_path", PERSONAL_PATH), ("email_candidate", EMAIL)):
            matches = list(rule.finditer(data))
            if key == "email_candidate":
                matches = [m for m in matches if not is_email_fixture(data, m)]
            if matches:
                lines = sorted({data.count(b"\n", 0, m.start()) + 1 for m in matches})
                critical[key].append((name, lines[:8]))
    if staged:
        try:
            email = git("config", "--get", "user.email").strip().lower()
        except subprocess.CalledProcessError:
            email = b""
        if not email.endswith(b"@users.noreply.github.com"):
            critical["git_author_not_private_noreply"].append(("<local-config>", []))
    print("scan_mode", "INDEX" if staged else "WORKING_TREE")
    print("candidate_files", len(paths), "text_files", scanned)
    for section_name, grouped in (("CRITICAL", critical), ("ADVISORY", warnings)):
        for kind, rows in sorted(grouped.items()):
            print(section_name, kind, "files", len(rows))
            for name, locations in rows[:16]:
                if kind != "binary_pixels_not_inspected":
                    print("  candidate", name, "line_numbers_or_metadata", locations)
    if critical:
        print("FAIL: do not commit until these findings are resolved")
        return 1
    print("PASS: no high-confidence private path, PII or credential pattern in candidates")
    print("LIMITATION: image pixels, unpublished Git objects, GitHub PR/logs and unusual secrets need independent review")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    sys.exit(inspect(args.staged))
