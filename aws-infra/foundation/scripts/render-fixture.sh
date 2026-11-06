#!/usr/bin/env bash
set -euo pipefail
foundation_dir="$(cd "$(dirname "$0")/.." && pwd)"
repo_dir="$(cd "$foundation_dir/../.." && pwd)"
exec "$repo_dir/.venv/bin/python" "$foundation_dir/scripts/integration.py" --render "$@"
