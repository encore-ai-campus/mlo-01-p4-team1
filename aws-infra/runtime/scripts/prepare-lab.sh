#!/usr/bin/env bash
set -euo pipefail
runtime_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 -m venv "$runtime_dir/.venv"
"$runtime_dir/.venv/bin/python" -m pip install --no-cache-dir -r "$runtime_dir/requirements.lock.txt"
"$runtime_dir/.venv/bin/python" "$runtime_dir/scripts/fetch-jars.py"
docker build --platform "${RUNTIME_LAB_PLATFORM:-linux/arm64}" --label mlo.role=B \
  -f "$runtime_dir/containers/host-lab.Dockerfile" -t mlo-p4-b-host-lab:20261029 "$runtime_dir"
