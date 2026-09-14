#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"
cd_repo

say "Repository"
printf 'root:   %s\n' "$(pwd)"
printf 'branch: %s\n' "$(git branch --show-current)"
printf 'HEAD:   %s\n' "$(git rev-parse HEAD)"
git status --short

say "Required governance files"
for f in AGENTS.md CLAUDE.md rules.md docs/README.md docs/ROADMAP.md docs/TESTING.md docs/DECISIONS.md; do
  require_file "$f"
  printf 'OK %s\n' "$f"
done

say "Python"
if [[ -x ".venv/bin/python" ]]; then
  .venv/bin/python --version
  printf 'venv: %s\n' "$(readlink -f .venv)"
else
  printf 'project .venv not found\n'
  python3 --version || true
fi

say "Tools"
for cmd in git python3; do
  if command -v "$cmd" >/dev/null 2>&1; then
    printf 'OK %-12s %s\n' "$cmd" "$(command -v "$cmd")"
  else
    printf 'MISSING %s\n' "$cmd"
  fi
done
for cmd in pw-dump pw-cat pactl bluetoothctl; do
  if command -v "$cmd" >/dev/null 2>&1; then
    printf 'OK %-12s %s\n' "$cmd" "$(command -v "$cmd")"
  else
    printf 'OPTIONAL/MISSING %s\n' "$cmd"
  fi
done

say "Current phase status"
grep -n -A12 -B2 "Phase 4 status update" docs/ROADMAP.md || true

if [[ "${1:-}" == "--runtime-env" ]]; then
  say "Runtime environment presence (values are NOT printed)"
  for name in DEEPGRAM_API_KEY GROQ_API_KEY ELEVENLABS_API_KEY LLM_MODEL; do
    if [[ -n "${!name:-}" ]]; then
      printf 'SET   %s\n' "$name"
    else
      printf 'UNSET %s\n' "$name"
    fi
  done
fi
