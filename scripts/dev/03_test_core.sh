#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"
cd_repo
ensure_venv_python

PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -m pytest -q \
  tests/test_update.py \
  tests/test_player.py \
  tests/test_carrier.py \
  tests/test_turn_completion.py \
  -p no:cacheprovider
