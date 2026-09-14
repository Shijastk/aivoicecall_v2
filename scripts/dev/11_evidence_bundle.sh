#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"
cd_repo

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${EVIDENCE_DIR:-evidence/$STAMP}"
mkdir -p "$OUT"

{
  echo "timestamp_local=$(date --iso-8601=seconds)"
  echo "branch=$(git branch --show-current)"
  echo "head=$(git rev-parse HEAD)"
  echo
  git status --short
  echo
  git log --oneline -5
} > "$OUT/git.txt"

{
  python3 --version 2>&1 || true
  if [[ -x .venv/bin/python ]]; then
    .venv/bin/python --version 2>&1 || true
  fi
  uname -a
} > "$OUT/environment.txt"

{
  grep -n -A14 -B2 "Phase 4 status update" docs/ROADMAP.md || true
  echo
  grep -n -A40 -B3 "Controlled Phase 4B live shadow result" \
    docs/phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md || true
} > "$OUT/phase-status.txt"

cat > "$OUT/notes.md" <<'EOF'
# Evidence notes

Authorization/scope:

Commands executed:

Results:

Known limitations:

Rollback result:

Decision / next gate:
EOF

printf 'Created sanitized evidence directory: %s\n' "$OUT"
printf 'No .env or credential file was copied.\n'
