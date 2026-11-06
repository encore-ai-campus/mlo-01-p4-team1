#!/usr/bin/env bash
set -euo pipefail
foundation_dir="$(cd "$(dirname "$0")/.." && pwd)"
repo_dir="$(cd "$foundation_dir/../.." && pwd)"
python_bin="$repo_dir/.venv/bin/python"
[ -x "$python_bin" ] || { echo 'BLOCKED: .venv 및 requirements-common.lock.txt가 필요합니다.' >&2; exit 2; }
case "${1:-}" in
 unit) "$python_bin" "$repo_dir/scripts/required_suite.py" --start "$foundation_dir/tests" --pattern 'test_unit.py'; "$python_bin" "$repo_dir/scripts/required_suite.py" --start "$foundation_dir/tests" --pattern 'test_auth.py'; "$python_bin" "$repo_dir/scripts/required_suite.py" --start "$foundation_dir/tests" --pattern 'test_track2.py'; exec "$python_bin" "$repo_dir/scripts/required_suite.py" --start "$foundation_dir/tests" --pattern 'test_setup.py' ;;
 integration) exec "$python_bin" "$foundation_dir/scripts/integration.py" ;;
 all) bash "$0" unit; bash "$0" integration ;;
 *) echo 'usage: check-local.sh unit|integration|all' >&2; exit 2 ;;
esac
