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

Phase 3 closeout status:

- reference-hardware device/call disappearance teardown: **validated**
- repeated lifecycle: **validated with 5 consecutive fresh start/stop cycles**
- no orphan `pw-cat` after normal or call-cut teardown: **validated**
- stronger distinction between supported EnumFormat capability and actual
  negotiated state: remains compatibility hardening
- measured/approved end-to-end production queue and latency budget: remains for
  integrated pipeline/E2E measurement
- long-run soak/reconnect/concurrency/coexistence: remains later resilience work
- broader device/codec compatibility: remains later compatibility qualification

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

### Phase 3 final closeout evidence — 2026-09-13

Repeated lifecycle test:

```text
CYCLE 1/5 ... SESSION STOPPED CLEANLY
CYCLE 2/5 ... SESSION STOPPED CLEANLY
CYCLE 3/5 ... SESSION STOPPED CLEANLY
CYCLE 4/5 ... SESSION STOPPED CLEANLY
CYCLE 5/5 ... SESSION STOPPED CLEANLY
ALL 5 CYCLES COMPLETED CLEANLY
```

During an active repeated-lifecycle cycle:

```text
pw-cat --record   --target bluez_input.00_C7_11_7B_84_21.0 ...
pw-cat --playback --target bluez_output.00_C7_11_7B_84_21.1 ...
```

After teardown:

```text
pgrep -a pw-cat
# no output
```

The first intentional call-cut test exposed a route-restore race:

```text
RouteIsolationError:
failed to restore PipeWire link ...
failed to link ports: No such file or directory
```

The process cleanup itself was already successful: `pgrep -a pw-cat` returned no
output. The failure was narrowed to BlueZ HFP port disappearance/recreation while
the session attempted to restore links.

The route-isolation stop path was hardened to inspect a fresh PipeWire graph and
retry restoration for a short bounded window. If the selected Bluetooth call port
remains absent for the full window, the old link is obsolete because the call
stream no longer exists. Other restoration failures continue to fail closed.

Final focused test selection:

```text
25 passed, 1 warning
```

Final real call-cut retest:

```text
Starting AI-only session...
SESSION STARTED
NOW CUT THE CELLULAR CALL while this is still running.
Stopping session after call cut...
SESSION STOPPED CLEANLY AFTER CALL CUT
```

Post-teardown:

```text
pgrep -a pw-cat
# no output
```

After hangup, BlueZ call nodes were absent from the graph and only normal local
audio endpoints remained, which is expected once the HFP call stream ends.

Latest full regression supplied before Phase 3 closeout:

```text
826 passed, 4 failed, 4 warnings
```

The four failing identities remain the same documented baseline failures:

```text
scripts/test_v2_keys.py::test_shunya_key
scripts/test_v2_keys.py::test_azure_key
tests/test_config_api.py::TestIsolation::test_the_call_server_has_no_config_routes
tests/test_test_call.py::TestTheProcessSplitSurvives::test_the_call_server_has_no_test_call_routes
```

No new Bluetooth-related failure identity was observed.

## Phase 4B shadow-speculation verification

```bash
python -m pytest -q tests/test_bluetooth_speculative.py tests/test_flux.py tests/test_bluetooth_conversation.py tests/test_bluetooth_production.py -p no:cacheprovider
python -m pytest -q tests/test_bluetooth_*.py -p no:cacheprovider
```

Only after offline tests pass should an explicitly authorized controlled live shadow run use both `--eager-eot-threshold` and `--shadow-speculation`. Shadow text must never appear in TTS, monitor transcript or committed history.

### Runtime result — 2026-09-14

Offline verification before the live run:

- focused Phase 4B selection: **32 passed, 3 warnings**
- Bluetooth regression selection: **86 passed, 3 warnings**

Controlled live Bluetooth shadow run:

- 10 final turns
- 0 `ready_before_final`
- 10 `not_ready_by_final`
- 4 resumed/retracted eager candidates
- one resumed candidate produced a shadow first token at 500.8ms and was
  correctly discarded after `TurnResumed`.

No speculative text was intentionally sent to TTS or committed conversation
history. This run validates the shadow/cancellation measurement path, not a
Phase 4C response-reuse path and not a sub-500ms caller-heard latency claim.

## TTS warm-pool regression — 2026-09-14

Historical unlimited-age implementation results, retained as evidence. That
policy was subsequently disproven by owner-supplied live input-timeout evidence;
the bounded policy and its new regression results are recorded below.

Executed in the working tree using the existing Python 3.12 `.venv`, with
`PYTHON_DOTENV_DISABLED=1` to prevent test imports from loading `.env`:

```bash
PATH="$PWD/.venv/bin:$PATH" PYTHON_DOTENV_DISABLED=1 python -m pytest -q \
  tests/test_tts_failure.py \
  tests/test_bluetooth_production.py \
  tests/test_bluetooth_conversation.py \
  -p no:cacheprovider
PYTHON_DOTENV_DISABLED=1 ./scripts/dev/04_test_bluetooth.sh
PYTHON_DOTENV_DISABLED=1 PYTEST_ADDOPTS='-o faulthandler_timeout=15' \
  ./scripts/dev/05_test_full.sh
```

Results:

- focused selection: **22 passed, 3 warnings in 0.86s**
- Bluetooth regression: **86 passed, 3 warnings in 1.34s**
- full regression: **873 passed, 4 failed, 4 warnings in 20.79s**

The initial sandboxed full run stalled around call-history/lifecycle tests and
was interrupted without a completed result. The full result above is the
authorized rerun outside the sandbox with a diagnostic traceback timer.

All four completed-run failures match the documented identities and signatures:

- `scripts/test_v2_keys.py::test_shunya_key`: unmarked async function unsupported
- `scripts/test_v2_keys.py::test_azure_key`: unmarked async function unsupported
- `tests/test_config_api.py::TestIsolation::test_the_call_server_has_no_config_routes`:
  `AttributeError: '_IncludedRouter' object has no attribute 'path'`
- `tests/test_test_call.py::TestTheProcessSplitSurvives::test_the_call_server_has_no_test_call_routes`:
  the same `_IncludedRouter.path` AttributeError

No new failing identity appeared. Warnings remain the existing websockets,
audioop and Starlette/AnyIO deprecations. No unrelated failures were changed.

`TestPoolLiveness` now verifies checkout past the old TTL, periodic retention of
healthy old sockets, idle death followed by automatic refill, callback/voice
binding, idempotent stop, interrupted preconnect cleanup, checkout during
eviction, and stop during detached-entry cleanup. Existing dead-checkout and
zero-audio completion coverage remains. All provider boundaries are fake in
these tests; no live calls or devices were used. **Live latency improvement is
NOT yet proven.** The later measurement command and limits are in the
[Phase 4 record](phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md#tts-warm-pool-follow-up--2026-09-14).

## Bounded TTS idle-policy regression — 2026-09-14

After the owner reported the 19,819ms silent turn, revised the policy to
`max_idle_age=15.0` with a separate health-check interval and expiry-aware
maintenance scheduling. The previous results above do not validate this policy.

Executed with the existing Python 3.12 environment, dotenv loading disabled:

```bash
PATH="$PWD/.venv/bin:$PATH" PYTHON_DOTENV_DISABLED=1 python -m pytest -q \
  tests/test_tts_failure.py \
  tests/test_bluetooth_production.py \
  tests/test_bluetooth_conversation.py \
  -p no:cacheprovider
PYTHON_DOTENV_DISABLED=1 ./scripts/dev/04_test_bluetooth.sh
PYTHON_DOTENV_DISABLED=1 ./scripts/dev/05_test_full.sh
```

- Focused: **35 passed, 3 warnings in 0.90s**.
- Bluetooth: **86 passed, 3 warnings in 1.34s**.
- Full: **886 passed, 4 failed, 4 warnings in 20.79s**.

The four failures are the same identities/signatures listed above: unsupported
unmarked async Shunya/Azure probes, and `_IncludedRouter.path` AttributeError in
the config/test-call isolation tests. No new failing identity or unrelated fix.

Tests prove warm reuse at 11 seconds, rejection at 15, 16 and 19.819 seconds,
proactive over-age replacement, expiry waking before a longer health interval,
dead checkout/idle behavior, callback/voice binding, finite positive policy
validation, and deterministic cleanup including preconnect/eviction interruption.
These tests use fake providers and no live calls/devices. Final max-idle choice
and latency improvement still require another controlled live validation.

## Initial TTS startup readiness regression — 2026-09-14

Executed after the first-turn race correction, using the same Python 3.12
environment and dotenv-disabled commands as the bounded idle-policy run above:

- Focused (`test_tts_failure`, `test_bluetooth_production`,
  `test_bluetooth_conversation`): **43 passed, 3 warnings in 1.01s**.
- `./scripts/dev/04_test_bluetooth.sh`: **89 passed, 3 warnings in 1.46s**.
- `./scripts/dev/05_test_full.sh`: **894 passed, 4 failed, 4 warnings in 20.84s**.

Failures remain exactly the two unsupported unmarked async Shunya/Azure probes
and the two `_IncludedRouter.path` route-isolation AttributeErrors, with the
previously recorded identities/signatures. No new failure identity appeared.

Delayed fake preconnection tests show that first checkout stays pending with
only one connection attempt, then receives that original socket. Production
wiring tests use the real TTSPool with fake TTS to verify Agent creation waits,
first acquisition retains the selected voice, and cancellation/provider failure
still stops pool resources. Readiness tests also cover timeout, pool stop and
waiter cancellation without cancelling the shared warmup. Existing 15-second
expiry/refill and cleanup regressions remain green.

No live calls/providers/devices were exercised. This is startup ordering and
resource-ownership evidence, not proof of improved live first-turn latency.

## Startup-readiness review corrections — 2026-09-14

Readiness now permits transient preconnection retries within the existing
timeout and chains the latest failure on timeout. The production idle timestamp
is immediately before the successful initialization send, after the handshake;
send/backpressure time still counts against the unchanged 15-second limit.

Ran the same three dotenv-disabled commands recorded above, using Python 3.12:

- Focused: **46 passed, 3 warnings in 5.26s**.
- Bluetooth: **89 passed, 3 warnings in 2.34s**.
- Full: **897 passed, 4 failed, 4 warnings in 25.13s**.
- `git diff --check`: passed.

Exact failure identities and signatures remain:

- `scripts/test_v2_keys.py::test_shunya_key`: unsupported unmarked async function.
- `scripts/test_v2_keys.py::test_azure_key`: unsupported unmarked async function.
- `tests/test_config_api.py::TestIsolation::test_the_call_server_has_no_config_routes`:
  `AttributeError: '_IncludedRouter' object has no attribute 'path'`.
- `tests/test_test_call.py::TestTheProcessSplitSurvives::test_the_call_server_has_no_test_call_routes`:
  the same `_IncludedRouter.path` AttributeError.

New tests verify recovery after a transient initial failure without a duplicate
cold checkout, production readiness through that retry, and the latest error
as timeout cause after repeated failures. A real TTSService with a fake socket
and clock verifies that a four-second handshake is excluded while a two-second
initialization send remains in the idle budget, including eviction at 15 seconds.
Existing cancellation, stop, expiry/refill and voice-binding coverage remains
green. No live calls/providers/devices were exercised; live latency and the final
idle-margin choice still need controlled validation.

## Final controlled TTS validation — 2026-09-14

Following the offline runs above, the task owner supplied final live validation
of the startup barrier and 15-second idle policy. Initial warmth preceded caller
audio forwarding; first-turn setup was 0ms versus the earlier 4132ms cold setup.
Warm reuse at 10.164s, 12.995s and 14.629s and proactive replacement at
15.000–15.001s were observed. No over-limit checkout or `input_timeout_exceeded`
occurred. Later `quota_exceeded` was provider-account exhaustion, unrelated to
the pool design. This is supplied runtime evidence, not an agent-executed run.

Only warm-connection/setup behavior is validated; no lower ElevenLabs synthesis
latency is claimed. Full scope and limits are owned by the
[Phase 4 validation record](phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md#final-controlled-tts-warm-pool-validation--2026-09-14).
This documentation-only update ran `git diff --check`; tests and live calls were
not rerun. Historical offline results and prior live failures remain preserved.
