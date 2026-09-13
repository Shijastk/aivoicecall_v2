# Testing and evidence policy

This document distinguishes inspected code, historical test evidence and executed
Phase 2 evidence. A green unit suite does not by itself prove live PipeWire,
Bluetooth, provider or cellular-call behavior.

## Existing organization

| Files | Coverage represented in source |
|---|---|
| `tests/test_update.py`, `test_state_phase1.py` | Pure transitions, inbound-only feed, carrier signal behavior |
| `tests/test_carrier.py`, `test_vobiz.py`, `test_integration.py` | Carrier abstractions/protocol/auth, fake-Vobiz transport, restart and disconnect |
| `tests/test_player.py` | Byte geometry, padding/clearing, real wall-clock pacing through TimingSession |
| `tests/test_flux.py`, `test_tts_failure.py`, `test_turn_completion.py` | Flux events, service failure handling, checkpoint grace/void/completion |
| `tests/test_config_store.py`, `test_runtime_config.py`, `test_config_api.py`, `test_voices.py` | Config validation/persistence/resolution, catalog and process seam |
| `tests/test_test_call.py`, `test_call_lifecycle.py` | Operator HTTP requests, lifecycle/proxy semantics |
| `tests/test_call_history.py`, `test_call_monitor.py` | Revisions/folding, live monitoring and bounded observation |
| `tests/test_recording.py`, `test_spool.py`, `test_notify.py`, `test_log.py` | Audio tee, deferred writes, notifications and logging |
| `scripts/test_v2_keys.py` | External Shunya/Azure probes; not offline fixtures |
| `tests/test_bluetooth_codec.py` | Bluetooth/SHUO format contracts, stateful rate conversion, fragmented samples, duration behavior |
| `tests/test_bluetooth_transport.py` | Bounded queue policies and structural inbound/outbound separation |
| `tests/test_bluetooth_pipewire.py` | Hardware-free capability matching, ambiguity rejection and platform gate |
| `tests/test_bluetooth_telephony.py` | Hardware-free telephony observer fake; no live D-Bus |
| `tests/test_bluetooth_runtime.py` | Ordered start, rollback and idempotent stop |

## Historical baseline — preserved history

Task-owner previously recorded historical result: **775 passed, 4 failed**.
That result remains historical evidence and is not replaced or rewritten.

Historical failing identities:

- `scripts/test_v2_keys.py::test_shunya_key`
- `scripts/test_v2_keys.py::test_azure_key`
- `tests/test_config_api.py::TestIsolation::test_the_call_server_has_no_config_routes`
- `tests/test_test_call.py::TestTheProcessSplitSurvives::test_the_call_server_has_no_test_call_routes`

Existing failures are not permission to fix unrelated code. Compare by test
identity and failure signature, never by count alone.

## Phase 2 executed evidence

Implementation revision:

```text
6f4c8c423a0d2741d033439c510fc21a1052a91e
```

Environment supplied by task owner for this execution:

```text
Repository: ~/Projects/aivoicecall_v2
Virtual environment active
Python runtime: project Python 3.12 environment
```

### Bluetooth-focused suite

Command:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_bluetooth_codec.py \
  tests/test_bluetooth_transport.py \
  tests/test_bluetooth_pipewire.py \
  tests/test_bluetooth_telephony.py \
  tests/test_bluetooth_runtime.py \
  -p no:cacheprovider
```

Result:

```text
27 passed, 1 warning
```

Warning:

```text
shuo/bluetooth/codec.py:
DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
```

### Relevant SHUO regression selection

Command:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_update.py \
  tests/test_player.py \
  tests/test_carrier.py \
  tests/test_turn_completion.py \
  -p no:cacheprovider
```

Result:

```text
99 passed, 2 warnings
```

### Full root suite

Command:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

Result:

```text
802 passed, 4 failed, 4 warnings
```

Observed full-suite failures:

```text
scripts/test_v2_keys.py::test_shunya_key
scripts/test_v2_keys.py::test_azure_key
tests/test_config_api.py::TestIsolation::test_the_call_server_has_no_config_routes
tests/test_test_call.py::TestTheProcessSplitSurvives::test_the_call_server_has_no_test_call_routes
```

Observed signatures:

- Shunya/Azure probe tests: async test functions collected without a suitable
  pytest async framework marker/plugin path for those functions.
- Config/test-call isolation tests: route enumeration encountered an
  `_IncludedRouter` object without a `.path` attribute.

These identities are the same four identities previously recorded as failures.
No new failure identity was observed from Phase 2. This document does not claim
the current signatures are necessarily identical to every historical run unless
a historical traceback is available.

## Phase 2 codec-specific evidence

The initial test assumption that every isolated 8 kHz → 16 kHz 20 ms conversion
must emit exactly 640 S16LE bytes was corrected after observing Python 3.12
`audioop.ratecv` behavior.

Stateful behavior verified by tests:

```text
first 160-byte µ-law chunk -> 638 S16LE bytes
subsequent 20 ms chunks     -> 640 S16LE bytes
```

A multi-frame duration test verifies this is a fixed one-sample interpolation
startup boundary rather than a 2-byte-per-frame accumulating drift. The codec
does not add fake padding solely to force chunk geometry.

## Bluetooth test layers

### Phase 2 — implemented/tested

Hardware-free coverage includes:

- supported format/rate/channel validation
- 16 kHz S16LE mono → 8 kHz µ-law conversion
- 8 kHz µ-law → 16 kHz S16LE mono conversion
- arbitrary/odd fragmented S16 input handling
- stateful converter continuity
- independent direction state
- explicit bounded queue overflow semantics
- no shared inbound/outbound queue object
- capability-based PipeWire target selection contracts
- ambiguity/no-match fail-closed behavior
- unsupported-platform rejection without import-time hardware access
- telephony fakes with no live D-Bus
- partial-start rollback and idempotent stop

No device access, real process streams or active-call audio is claimed.

### Phase 3 — implemented / reference-hardware validation executed

Executed Phase 3 evidence now confirms on the reference environment:

- real target discovery and direction for the active HFP call
- explicit `pw-cat` targeting of the selected Bluetooth nodes
- AI-only physical mic/speaker route isolation during the session
- restoration of the removed routes on stop
- clean capture/playback shutdown with no orphan `pw-cat`
- reproduction and fix of the unread-capture stdout shutdown timeout

Still pending for Phase 3 closeout / later qualification:

- stronger distinction between supported EnumFormat capability and actual negotiated state
- device disappearance handling
- measured/approved production queue and latency budget
- repeated/long-run hardware cycles beyond the executed validation
- broader device/codec compatibility

### Later phases

Phase 4: injected SHUO conversation integration.

Phase 5: separately authorized controlled cellular E2E, digital duplex,
audio integrity, STT self-audio/echo checks, latency and manual abort.

Phase 6: automated answer/hangup/remote-disconnect/cancellation and lifecycle.

Phase 7: reconnect, stale audio, concurrency, coexistence, overload,
resource leak and security/privacy.

Phase 8: supported installation, diagnostics, packaged startup and rollback.

## Measurements and completion evidence

Phase 2 intentionally does not claim live Bluetooth latency.

Production queue capacity/latency is still uncalibrated. `BoundedAudioQueue`
requires explicit capacity and overflow policy so no unbounded queue or silent
default policy is introduced. Phase 3 must choose and measure numeric budgets.

Measure capture-to-STT, EOT-to-first-token, token-to-first-TTS, conversion/queue
residence, first-uplink-sample and caller mouth-to-ear independently in the
appropriate later phases.

Use distinguishable uplink/downlink signals and prove outbound-only signals do
not reach STT. For cleanup, compare task/process/handle counts before/after start,
stop, partial failure and repeated cycles.

A phase-completion record needs scoped authorization, revision, commands,
fixtures, environment/capability versions, gate results, failure
identities/signatures, sanitized artifacts, known limitations, rollback result
and explicit approval to advance.

## Phase 3 executed evidence — 2026-09-13

Focused route/cleanup suite:

```text
21 passed, 1 warning
```

Full Bluetooth-focused suite after cleanup fix:

```text
51 passed, 1 warning
```

Warning:

```text
DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
```

Reference active-call lifecycle validation:

```text
Duration: 20 seconds
Start: clean
During session:
  explicit pw-cat record process present
  explicit pw-cat playback process present
  physical Mic1 -> Bluetooth uplink links absent
  Bluetooth downlink -> physical Speaker links absent
Stop: SESSION STOPPED CLEANLY
After stop:
  prior routes restored
  pgrep -a pw-cat returned no output
```

Important interpretation: the 20-second duration belongs to the test script's
`asyncio.sleep(20)`. It is not a runtime limit in the session resource. A
production integration should keep the session alive until call/session teardown.

The first hardware cleanup run failed with:

```text
shuo.bluetooth.process.ProcessError:
child process did not exit after terminate/kill
```

At the same time, `pgrep -a pw-cat` after teardown was empty. The failure was
therefore in asyncio subprocess cleanup/reaping with unread capture stdout, not
persistent orphan process ownership. The capture endpoint was changed to drain
stdout during shutdown only. Focused tests and the real 20-second lifecycle
validation passed after the change.
