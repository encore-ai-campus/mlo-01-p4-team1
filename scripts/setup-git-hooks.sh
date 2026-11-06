#!/bin/sh
# Team opt-in setup. Installs tracked hooks via a repository-relative hooksPath.
set -eu
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
test -x scripts/hooks/pre-commit
test -x scripts/hooks/pre-push

old_path="$(git config --local --get core.hooksPath || true)"
if [ -n "$old_path" ] && [ "$old_path" != "scripts/hooks" ]; then
  echo "Previous local hooksPath was configured. This setup will replace it: $old_path" >&2
fi
if [ -x "$(git rev-parse --git-path hooks)/pre-commit" ]; then
  echo "Note: an existing .git/hooks/pre-commit will be bypassed by hooksPath=scripts/hooks." >&2
fi
git config --local core.hooksPath scripts/hooks
test "$(git config --local --get core.hooksPath)" = "scripts/hooks"
echo "Hooks installed for this clone: commit secrets gate + shared-branch push guard."
echo "GitHub rules are NOT enforced server-side on the current Private plan."
