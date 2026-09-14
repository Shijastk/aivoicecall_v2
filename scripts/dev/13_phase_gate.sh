#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"
cd_repo

PHASE="${1:-}"
case "$PHASE" in
  1) FILE="docs/phases/PHASE_01_RUNTIME_CONTRACT.md" ;;
  2) FILE="docs/phases/PHASE_02_ADAPTER_AND_CODEC.md" ;;
  3) FILE="docs/phases/PHASE_03_PIPEWIRE_INTEGRATION.md" ;;
  4) FILE="docs/phases/PHASE_04_SHUO_PIPELINE_INTEGRATION.md" ;;
  5) FILE="docs/phases/PHASE_05_CELLULAR_E2E.md" ;;
  6) FILE="docs/phases/PHASE_06_CALL_CONTROL_AND_LIFECYCLE.md" ;;
  7) FILE="docs/phases/PHASE_07_RESILIENCE_AND_COEXISTENCE.md" ;;
  8) FILE="docs/phases/PHASE_08_RELEASE_AND_OPERATIONS.md" ;;
  *) die "usage: $0 <1-8>" ;;
esac

require_file "$FILE"
printf '=== %s ===\n' "$FILE"
sed -n '1,280p' "$FILE"

printf '\n=== Repository advancement policy ===\n'
grep -n -A18 "## Common advancement and rollback policy" docs/ROADMAP.md || true
