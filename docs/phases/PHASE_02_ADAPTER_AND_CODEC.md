# Bluetooth Phase 2 — Isolated adapter and codec

**Status: IMPLEMENTED / TESTED — completion closeout pending documentation sync and explicit queue-budget decision.**

Shared contracts: [Roadmap](../ROADMAP.md), [requirements](../REQUIREMENTS.md),
[Bluetooth architecture](../BLUETOOTH_ARCHITECTURE.md),
[current architecture](../CURRENT_ARCHITECTURE.md), [testing](../TESTING.md).

## Goal and included scope

Design and implement hardware-free Bluetooth contracts and directional conversion
without disturbing production imports.

Implemented scope includes injectable target/selection contracts, negotiated audio
format validation, separate capture/playback interfaces, stateful conversion and
reframing, bounded queues with explicit overflow policy, telephony fakes/interfaces,
partial-start rollback and idempotent stop.

## Explicit exclusions

Real PipeWire capture/playback, active-call audio, live device discovery, D-Bus
control, provider calls and core pipeline integration remain outside Phase 2.

## Dependencies and prerequisites

Phase 1 evidence was reviewed before implementation. No new production dependency
was added.

Phase 2 currently uses Python 3.12 stdlib `audioop` for µ-law conversion and
stateful rate conversion. `audioop` is deprecated and removed in Python 3.13;
a replacement for Python 3.13+ requires a separately reviewed dependency or
implementation decision.

## Files and boundaries

**Implemented Phase 2 files:**

```text
shuo/bluetooth/__init__.py
shuo/bluetooth/codec.py
shuo/bluetooth/pipewire.py
shuo/bluetooth/runtime.py
shuo/bluetooth/telephony.py
shuo/bluetooth/transport.py

tests/test_bluetooth_codec.py
tests/test_bluetooth_pipewire.py
tests/test_bluetooth_runtime.py
tests/test_bluetooth_telephony.py
tests/test_bluetooth_transport.py
```

`bluetooth_main.py` was intentionally not added in Phase 2. A live opt-in entrypoint
belongs to a later phase after the process/runtime integration design is approved.

Phase 2 `telephony.py` contains interfaces/fakes only: no live D-Bus access or call
control. `pipewire.py` contains hardware-free target description, capability
matching, explicit selection and platform validation; real discovery/process stream
integration belongs to Phase 3.

Reference `shuo/carrier/base.py`, `shuo/types.py`, `shuo/services/player.py`,
`flux.py` and `tts.py` remain unchanged by Phase 2.

S16LE 16 kHz mono is allowed only inside the isolated Bluetooth boundary, with
conversion to/from SHUO µ-law 8 kHz. `rules.md` C1/C2 remain mandatory for the
carrier/shared core; no PCM/L16 carrier route or PCM leakage is permitted.

The future PipeWire adapter may be Linux-specific only behind the isolated optional
boundary. Default production entrypoints must not import/start it, and core/carrier
Windows/Linux portability remains mandatory.

## Implemented codec behavior

Inbound:

```text
Bluetooth S16LE / 16 kHz / mono
→ stateful 16 kHz → 8 kHz conversion
→ G.711 µ-law / 8 kHz / mono
```

Outbound:

```text
SHUO G.711 µ-law / 8 kHz / mono
→ linear PCM
→ stateful 8 kHz → 16 kHz conversion
→ Bluetooth S16LE / 16 kHz / mono
```

Fragmented reads and incomplete S16 sample boundaries are handled explicitly.
Inbound and outbound conversion state is separate.

The Python 3.12 `audioop.ratecv` 8 kHz → 16 kHz stream emits one fewer output
sample at initial interpolation startup (638 bytes for the first 20 ms S16LE
output chunk, then 640-byte chunks). Tests verify that this is a fixed startup
boundary rather than progressive duration drift; no fake padding is added merely
to force per-call chunk geometry.

## Queue and direction contract

Inbound and outbound queues are structurally distinct. The same queue object cannot
be used for both directions.

`BoundedAudioQueue` requires callers to provide:

- `max_frames`
- an explicit overflow policy (`REJECT_NEW` or `DROP_OLDEST`)

Phase 2 intentionally does **not** hard-code a production latency/queue budget.
The numeric runtime queue budget must be measured and approved during Phase 3
hardware/runtime work. This avoids silently inventing a production latency target.

## Executed test evidence

Implementation revision:

```text
6f4c8c423a0d2741d033439c510fc21a1052a91e
```

Bluetooth-focused Phase 2 suite:

```text
27 passed, 1 warning
```

Relevant SHUO regression selection:

```text
99 passed, 2 warnings
```

Full root suite:

```text
802 passed, 4 failed, 4 warnings
```

The four full-suite failure identities match the pre-existing recorded baseline:

```text
scripts/test_v2_keys.py::test_shunya_key
scripts/test_v2_keys.py::test_azure_key
tests/test_config_api.py::TestIsolation::test_the_call_server_has_no_config_routes
tests/test_test_call.py::TestTheProcessSplitSurvives::test_the_call_server_has_no_test_call_routes
```

No new failure identity was observed from Phase 2.

The Bluetooth-focused warning is the Python 3.12 `audioop` deprecation warning.
It is recorded as a compatibility/maintenance limitation, not ignored as evidence
of Python 3.13 support.

## Acceptance state

The deterministic hardware-free Phase 2 implementation and tests are green.
Formal Phase 2 closeout requires this documentation sync and an explicit decision
on how the production numeric queue budget is advanced into Phase 3.

No real device, live PipeWire process, D-Bus call control, provider call or SHUO
conversation integration is claimed by these Phase 2 results.

## Risks / known limitations

- `audioop` is Python-3.12-era technical debt and is removed in Python 3.13.
- Unit tests cannot establish real PipeWire latency, routing, mSBC transport
  behavior, echo isolation or device-loss behavior.
- Production queue capacity/latency has not yet been calibrated.
- Lossy µ-law conversion must not be tested as byte-exact PCM round-trip.

## Rollback

Remove/disable only the newly introduced `shuo/bluetooth/` Phase 2 modules and
their tests. Existing carrier/V2 defaults require no system-state restoration.

## Approval and stop boundary

Phase 3 requires separate explicit authorization for real PipeWire/device/runtime
access. Phase 2 completion does not implicitly authorize active-call audio,
D-Bus call control, provider calls or later-phase integration.
