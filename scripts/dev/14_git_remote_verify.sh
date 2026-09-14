#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"
cd_repo

REMOTE="${1:-origin}"
BRANCH="${2:-$(git branch --show-current)}"

say "Local"
printf 'branch=%s\n' "$BRANCH"
printf 'head=%s\n' "$(git rev-parse HEAD)"
git status --short

say "Remote refs"
git fetch "$REMOTE" "$BRANCH" --quiet
printf 'remote=%s/%s\n' "$REMOTE" "$BRANCH"
printf 'remote_head=%s\n' "$(git rev-parse "$REMOTE/$BRANCH")"

say "Ahead/behind"
git rev-list --left-right --count "$REMOTE/$BRANCH...HEAD"

say "Recent commits"
git log --oneline --decorate -8
