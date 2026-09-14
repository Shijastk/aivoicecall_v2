#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"
cd_repo

printf '%s\n' "=== ROADMAP phase table ==="
grep -n -A14 "## Phase status" docs/ROADMAP.md || true

printf '\n%s\n' "=== Current Phase 4 update ==="
grep -n -A18 "## Phase 4 status update" docs/ROADMAP.md || true

printf '\n%s\n' "=== Phase 4 realtime status ==="
sed -n '1,12p' docs/phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md
