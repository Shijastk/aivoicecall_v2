#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"
cd_repo

say "Git snapshot"
git status --short
git log --oneline -5
printf 'branch: %s\n' "$(git branch --show-current)"
printf 'HEAD: %s\n' "$(git rev-parse HEAD)"

say "Diff stat"
git diff --stat || true
