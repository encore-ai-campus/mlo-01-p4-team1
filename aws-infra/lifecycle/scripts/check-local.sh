#!/usr/bin/env bash
set -euo pipefail
lifecycle_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$lifecycle_dir/.venv/bin/python"
case "${1:-}" in unit|integration|all) ;; *) echo 'usage: check-local.sh unit|integration|all' >&2; exit 2 ;; esac
[ -x "$python_bin" ] || { echo 'NOT READY: C 전용 .venv가 없습니다.' >&2; exit 2; }
"$python_bin" -c 'import sys,jsonschema;sys.exit(0 if sys.version_info[:3] == (3,14,8) else 2)' || { echo 'NOT READY: Python 3.14.8 및 requirements.lock.txt가 필요합니다.' >&2; exit 2; }
if [ "$1" != unit ]; then
  [ -x "$lifecycle_dir/.cache/toolchain/terraform" ] || { echo 'NOT READY: C 전용 Terraform 1.11.4가 없습니다.' >&2; exit 2; }
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$lifecycle_dir/src:$lifecycle_dir/tests"
export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true
unset AWS_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN || true
exec "$python_bin" "$lifecycle_dir/scripts/run-tests.py" "$1"
