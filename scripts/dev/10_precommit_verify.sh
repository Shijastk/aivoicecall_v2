#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"
cd_repo
ensure_venv_python

say "Worktree"
git status --short

say "Diff stat"
git diff --stat

say "Phase 4B / Bluetooth focused tests"
"$HERE/06_test_phase4b.sh"
"$HERE/04_test_bluetooth.sh"

say "Reminder"
printf '%s\n' \
  "Review docs ownership before commit." \
  "Do not commit/push unless the current task explicitly authorizes it." \
  "If architecture/interfaces/phase evidence changed, update owning docs in the same task."
