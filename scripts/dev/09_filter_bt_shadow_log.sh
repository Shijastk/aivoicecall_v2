#!/usr/bin/env bash
set -euo pipefail
LOG_PATH="${BT_SHADOW_LOG:-/tmp/bt-shadow-4b.log}"
[[ -f "$LOG_PATH" ]] || { echo "log not found: $LOG_PATH" >&2; exit 1; }

grep -Ei \
'BTShadow|Eager EOT measurement|BTLatency|BTLifecycle|Lifecycle: turn=|LLM first token|TTS first audio|Playback dispatched|TTSPool: Evicted stale|Turn cancelled' \
"$LOG_PATH"
