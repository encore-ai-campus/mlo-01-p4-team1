#!/usr/bin/env bash
set -euo pipefail
runtime_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$runtime_dir/src${PYTHONPATH:+:$PYTHONPATH}"
export ANSIBLE_CONFIG="$runtime_dir/ansible/ansible.cfg"
export ANSIBLE_HOME="$runtime_dir/.artifacts/ansible"
export ANSIBLE_LOCAL_TEMP="$runtime_dir/.artifacts/ansible/tmp"
export ANSIBLE_COLLECTIONS_PATH="$runtime_dir/.artifacts/collections"
export XDG_CACHE_HOME="$runtime_dir/.cache"
exec "${RUNTIME_PYTHON:-$runtime_dir/.venv/bin/python}" -m mlo_runtime.check "$@"
