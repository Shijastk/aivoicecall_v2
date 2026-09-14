#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"
cd_repo
require_cmd python3

if [[ -d ".venv" ]]; then
  die ".venv already exists; this script refuses to replace it automatically"
fi

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

printf '\nCreated .venv. Activate with:\n  source .venv/bin/activate\n'
