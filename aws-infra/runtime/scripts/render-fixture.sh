#!/usr/bin/env bash
set -euo pipefail
runtime_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$runtime_dir/src${PYTHONPATH:+:$PYTHONPATH}"
exec "${RUNTIME_PYTHON:-$runtime_dir/.venv/bin/python}" -m mlo_runtime.render "$@"
