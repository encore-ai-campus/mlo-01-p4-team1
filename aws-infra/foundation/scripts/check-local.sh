#!/usr/bin/env bash
set -euo pipefail
foundation_dir="$(cd "$(dirname "$0")/.." && pwd)"
repo_dir="$(cd "$foundation_dir/../.." && pwd)"
python_bin="$repo_dir/.venv/bin/python"
[ -x "$python_bin" ] || { echo 'BLOCKED: .venv 및 requirements-common.lock.txt가 필요합니다.' >&2; exit 2; }
case "${1:-}" in
 unit) "$python_bin" -m unittest discover -s "$foundation_dir/tests" -p 'test_unit.py' -v; "$python_bin" -m unittest discover -s "$foundation_dir/tests" -p 'test_auth.py' -v; exec "$python_bin" -m unittest discover -s "$foundation_dir/tests" -p 'test_setup.py' -v ;;
 integration) exec "$python_bin" "$foundation_dir/scripts/integration.py" ;;
 all) bash "$0" unit; bash "$0" integration ;;
 *) echo 'usage: check-local.sh unit|integration|all' >&2; exit 2 ;;
esac
