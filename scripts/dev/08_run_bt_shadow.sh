#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"
cd_repo
ensure_venv_python

[[ "${LIVE_CALL_AUTHORIZED:-}" == "YES" ]] || die "set LIVE_CALL_AUTHORIZED=YES only after explicit live-call/device/provider authorization"
[[ -n "${BT_ADDRESS:-}" ]] || die "set BT_ADDRESS, e.g. export BT_ADDRESS=AA:BB:CC:DD:EE:FF"
[[ -n "${PW_LATENCY:-}" ]] || die "set PW_LATENCY, e.g. export PW_LATENCY=120ms"
[[ -n "${DEEPGRAM_API_KEY:-}" ]] || die "DEEPGRAM_API_KEY is not loaded"
[[ -n "${GROQ_API_KEY:-}" ]] || die "GROQ_API_KEY is not loaded"
[[ -n "${ELEVENLABS_API_KEY:-}" ]] || die "ELEVENLABS_API_KEY is not loaded"
[[ -n "${LLM_MODEL:-}" ]] || die "set LLM_MODEL explicitly for measured runs"

LOG_PATH="${BT_SHADOW_LOG:-/tmp/bt-shadow-4b.log}"
CALL_ID="${BT_CALL_ID:-bt-shadow-4b}"
THRESHOLD="${EAGER_EOT_THRESHOLD:-0.3}"

printf 'Starting explicit Phase-4B shadow run.\n'
printf 'This script does NOT answer/originate/hang up the cellular call.\n'
printf 'model=%s latency=%s threshold=%s log=%s\n' "$LLM_MODEL" "$PW_LATENCY" "$THRESHOLD" "$LOG_PATH"

SHUO_LOG_LEVEL=INFO PYTHONPATH=. "$PYTHON" -c '
from shuo.log import setup_logging
setup_logging()
import runpy
runpy.run_path("scripts/run_bluetooth_ai.py", run_name="__main__")
' \
  --bluetooth-address "$BT_ADDRESS" \
  --latency "$PW_LATENCY" \
  --eager-eot-threshold "$THRESHOLD" \
  --shadow-speculation \
  --call-id "$CALL_ID" \
  2>&1 | tee "$LOG_PATH"
