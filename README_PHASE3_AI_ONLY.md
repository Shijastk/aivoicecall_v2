# Phase 3 AI-only route isolation patch

Base reviewed revision:

`97076cd1739e2456843ced239eeebeedcfefa70b`

This patch is additive. It does **not** modify `main.py`, `shuo/server.py`, carrier
code, browser/V2, providers, or existing Phase-3 files.

## Added files

- `shuo/bluetooth/routes.py`
  - parses the live `pw-link -l` graph
  - finds only physical ALSA microphone -> selected Bluetooth uplink links
  - finds only selected Bluetooth downlink -> physical ALSA playback links
  - snapshots and disconnects those links for AI-only mode
  - verifies isolation before media starts
  - restores only links it removed
  - owns partial-start rollback and retryable restore failures

- `shuo/bluetooth/phase3_session.py`
  - composes route isolation + existing capture endpoint + existing playback endpoint
  - start order: routes -> capture -> playback
  - stop order: playback -> capture -> routes
  - intentionally not wired into SHUO conversation; that remains Phase 4

- `tests/test_bluetooth_routes.py`
- `tests/test_bluetooth_phase3_session.py`

## Extract

From the repository root:

```bash
unzip ~/Downloads/phase3_ai_only_route_isolation.zip -d .
```

(or use the actual downloaded ZIP path).

## Focused tests

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_bluetooth_routes.py \
  tests/test_bluetooth_phase3_session.py \
  tests/test_bluetooth_process.py \
  tests/test_bluetooth_pipewire_live.py \
  tests/test_bluetooth_pipewire.py \
  tests/test_bluetooth_runtime.py \
  tests/test_bluetooth_transport.py \
  tests/test_bluetooth_codec.py \
  -p no:cacheprovider
```

Do not start Phase 4 based only on these tests. Hardware validation still needs to
prove automatic unlink, clean audio, restoration, repeated cycles and device-loss
behavior on the reference machine.
