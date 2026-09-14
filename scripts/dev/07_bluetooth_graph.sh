#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"
cd_repo

require_cmd pw-dump

say "BlueZ / telephony PipeWire nodes"
pw-dump | grep -E '"node.name"|"factory.name"|bluez_(input|output)|api.bluez5.sco' || true

say "pw-cat processes"
pgrep -a pw-cat || true

printf '\nNote: HFP call nodes may only exist while a cellular call is active.\n'
