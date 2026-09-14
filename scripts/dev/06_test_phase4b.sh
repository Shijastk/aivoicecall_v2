#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"
cd_repo
ensure_venv_python

"$PYTHON" -m pytest -q \
  tests/test_bluetooth_speculative.py \
  tests/test_flux.py \
  tests/test_bluetooth_conversation.py \
  tests/test_bluetooth_production.py \
  -p no:cacheprovider
