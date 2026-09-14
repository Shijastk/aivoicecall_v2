#!/usr/bin/env bash
set -euo pipefail

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

repo_root() {
  git rev-parse --show-toplevel 2>/dev/null || die "run from inside the aivoicecall_v2 git repository"
}

cd_repo() {
  local root
  root="$(repo_root)"
  cd "$root"
}

require_file() {
  [[ -f "$1" ]] || die "required file missing: $1"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command missing: $1"
}

say() {
  printf '\n== %s ==\n' "$*"
}

ensure_venv_python() {
  if [[ -x ".venv/bin/python" ]]; then
    PYTHON=".venv/bin/python"
  elif command -v python >/dev/null 2>&1; then
    PYTHON="$(command -v python)"
  else
    die "no project .venv Python and no python command found"
  fi
}
