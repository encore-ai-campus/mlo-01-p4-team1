"""Git security/privacy release gate. Prints paths and line numbers, never matched secrets.

From the integration-worktree root:
  python3 app/docker/check_git_candidates.py
  git add -- app
  python3 app/docker/check_git_candidates.py --staged

--staged inspects the ACTUAL INDEX CONTENT that will enter a commit, not the
possibly different working tree. Also review Git authors/history and screenshot pixels.
"""
import argparse
from collections import defaultdict
from pathlib import Path
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]

CREDENTIAL_PATTERNS = {
    "aws_access_key": rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "github_token": rb"\b(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{40,})\b",
    "slack_token": rb"\bxox[baprs]-[0-9A-Za-z-]{20,}\b",
    "discord_webhook": rb"https?://(?:discord|discordapp)\.com/api/webhooks/[0-9]{15,}/[A-Za-z0-9_-]{30,}",
    "openai_token": rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{35,}\b",
    "google_api_key": rb"\bAIza[A-Za-z0-9_-]{35}\b",
    "private_key": rb"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----",
    "jwt": rb"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b",
    "korean_rrn_hyphenated": rb"(?<!\d)\d{6}-[1-4]\d{6}(?!\d)",
    "korean_mobile_hyphenated": rb"(?<!\d)01[016789]-\d{3,4}-\d{4}(?!\d)",
    "absolute_user_home": rb"/Users/[A-Za-z0-9_.-]+/|C:\\Users\\[A-Za-z0-9_.-]+\\|C:/Users/[A-Za-z0-9_.-]+/",
}
ADVISORY = {
    "credential_url": rb"(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)://[^\s\"']{1,100}:[^\s\"'@]{8,120}@",
    "possible_email": rb"(?<![\w.+-])[A-Za-z0-9._%+-]{1,70}@[A-Za-z0-9.-]{2,80}\.[A-Za-z]{2,12}\b",
}
BAD_DIRS = {".private", ".aws", ".local", ".local-preview-logs", ".git", "node_modules"}
BAD_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".tfstate", ".sqlite", ".sqlite3"}
ALLOWED_ENVS = {".env.example", ".env.sample", ".env.template"}
UNREVIEWED_DIRS = {
    ("app", "docs", "acceptance"),
    ("app", "docs", "assets", "mockups"),
    ("app", "docs", "handoff", "infra"),
    ("app", "docs", "handoff", "weather", "received"),
}
PNG_DROP_CHUNKS = (b"eXIf", b"iTXt", b"tEXt", b"zTXt", b"tIME")


def git_output(args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def files(staged=False):
    if staged:
        raw = git_output(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z", "--", "app"])
    else:
        raw = git_output(["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "app"])
    return sorted({Path(os.fsdecode(item)) for item in raw.split(b"\0") if item})


def contents(name, staged):
    if staged:
        return git_output(["show", ":" + name.as_posix()])
    return (ROOT / name).read_bytes()


def maybe_email_real(data, hit):
    value = hit.group(0).lower()
    host = value.rsplit(b"@", 1)[-1]
    if host in (b"example.com", b"example.org", b"example.net", b"localhost"):
        return False
    if host.endswith((b".example", b".test", b".invalid", b"noreply.github.com")):
        return False
    start = data.rfind(b"\n", 0, hit.start()) + 1
    end = data.find(b"\n", hit.start())
    line = data[start:end if end != -1 else None].lower()
    # Web URL userinfo, systemd template units, and deliberately invalid URL tests are not email addresses.
    if b"systemctl" in line or b"journalctl -u" in line or b"service@" in line or b"@." in line:
        return False
    if any(marker in line for marker in (b"https://user:", b"http://user:", b"mongodb://user:", b"mongodb://u:", b"/ops/app/dashboards#")):
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Inspect index blobs exactly as committed")
    opts = parser.parse_args()
    candidates = files(opts.staged)
    if opts.staged and not candidates:
        print("FAIL: there are no staged app changes; stage explicitly, then re-check")
        return 2
    critical = defaultdict(list)
    advisory = defaultdict(list)
    images = []
    sizes = []
    total = 0
    for name in candidates:
        if not opts.staged and not (ROOT / name).is_file():
            continue
        try:
            raw = contents(name, opts.staged)
        except (OSError, subprocess.CalledProcessError):
            critical["unreadable_candidate"].append((str(name), []))
            continue
        total += len(raw)
        parts = name.parts
        if (any(segment in BAD_DIRS for segment in parts) or parts[:2] == ("app", "analysis") or any(parts[:len(mark)] == mark for mark in UNREVIEWED_DIRS)):
            critical["sensitive_or_unreviewed_directory"].append((str(name), []))
        if (name.name.startswith(".env") and name.name not in ALLOWED_ENVS) or name.suffix.lower() in BAD_SUFFIXES:
            critical["sensitive_file_extension"].append((str(name), []))
        if name.name == "spotify_dataset.csv":
            critical["explicitly_excluded_music_dataset"].append((str(name), []))
        if len(raw) >= 10 * 1024 * 1024:
            sizes.append((str(name), round(len(raw) / 1048576, 2)))
        if len(raw) > 30 * 1024 * 1024:
            critical["oversize_unscanned"].append((str(name), []))
            continue
        if name.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            images.append(str(name))
            if name.suffix.lower() == ".png":
                if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    critical["invalid_png_header"].append((str(name), []))
                else:
                    # Check PNG chunk types, not accidental text coincidences in compressed pixels.
                    at = 8
                    while at + 12 <= len(raw):
                        n = int.from_bytes(raw[at:at + 4], "big")
                        if at + 12 + n > len(raw):
                            critical["invalid_png_chunk"].append((str(name), []))
                            break
                        kind = raw[at + 4:at + 8]
                        if kind in PNG_DROP_CHUNKS:
                            critical["png_embedded_metadata"].append((str(name), []))
                            break
                        at += n + 12
                        if kind == b"IEND":
                            break
            continue
        if b"\x00" in raw[:2000]:
            advisory["binary_unexamined"].append((str(name), []))
            continue
        for group, patterns in ((critical, CREDENTIAL_PATTERNS), (advisory, ADVISORY)):
            for label, expr in patterns.items():
                matches = list(re.finditer(expr, raw))
                if label == "possible_email":
                    matches = [m for m in matches if maybe_email_real(raw, m)]
                if matches:
                    lines = sorted({raw.count(b"\n", 0, match.start()) + 1 for match in matches})
                    group[label].append((str(name), lines[:10]))
    print("MODE=", "STAGED_GIT_INDEX" if opts.staged else "WORKTREE_CANDIDATES")
    print("files=", len(candidates), "total_mib=", round(total / 1048576, 2))
    print("visual_image_files_to_review=", len(images))
    for group_name, group in (("CRITICAL", critical), ("ADVISORY", advisory)):
        for label, found in sorted(group.items()):
            print(group_name, label, "files=", len(found))
            for path, lines in found[:20]:
                print("  path=", path, "lines=", lines)
    for path, size in sizes:
        print("REVIEW_LARGE_FILE:", path, size, "MiB")
    if critical:
        print("FAIL: sensitive/unsafe content is eligible for commit; remove or sanitize")
        return 1
    print("PASS: no high-confidence credential/PII pattern or blocked file path found")
    print("WARNING: advisory findings and screenshot pixels still require human review")
    print("WARNING: this does not examine remote commit author emails or existing Git history")
    return 0


if __name__ == "__main__":
    sys.exit(main())
