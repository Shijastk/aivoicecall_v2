# Testing and evidence policy

No tests were run in this documentation task. **VERIFIED IN CODE** means inspected
assertions/fixtures, never a current green suite. No services, providers or hardware
were contacted. Dependencies were not installed.

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

`tests/conftest.py` adds repository/scripts to import paths and autouse-isolates
history/recordings with tmp_path and monkeypatch. `test_integration.py` uses
FastAPI TestClient, StubFlux, StubTTSPool and `scripts/fake_vobiz.py::VobizProtocol`;
`test_player.py::FakeSession`/`TimingSession` capture sends and timing. Inspect
fixture isolation before executing any selection; shared fixtures are not a
universal guarantee against every provider call or import-time environment load.

## Commands found in the repository

Root `README.md` testing section lists these exact commands; `CLAUDE.md` and
`docs/phase8-plan.md` also list the verbose suite command:

```bash
python -m pytest tests/test_update.py -v
python -m pytest tests/ -v
python -m pytest tests/ -q
```

These are documented commands, not executions in this task. The shell used for
text generation here had no `python` command; `python3` was available. Interpreter,
virtual environment and installed-package readiness for pytest remain UNKNOWN.
Do not install or alter dependencies to resolve that during documentation work.

**PROPOSED focused selections** for later authorized code work (same runner,
actual files; not claimed as pre-existing runbook commands):

```bash
python -m pytest tests/test_carrier.py tests/test_vobiz.py -v
python -m pytest tests/test_player.py tests/test_turn_completion.py tests/test_tts_failure.py -v
python -m pytest tests/test_integration.py tests/test_runtime_config.py -v
```

Choose only the affected group first, then full `tests/` regression when the task
authorizes it. Do not run bare root-wide pytest casually: it can discover
`scripts/test_v2_keys.py`. `main.py`, fake-carrier services, benchmark scripts,
`/bench/ttft` and provider probes are execution/integration tools, not non-mutating
Markdown validation. They were not authorized here.

## Historical baseline — NOT reproduced

Task-owner recorded historical result: **775 passed, 4 failed**.
Command, revision, interpreter/dependency versions, date and failure signatures
were not supplied. It is not the current baseline and cannot be equated with
`python -m pytest tests/ -v`, because two named failures are under `scripts/`.

| Historical failing identity | Current evidence |
|---|---|
| `scripts/test_v2_keys.py::test_shunya_key` | Function exists; historical signature/cause UNKNOWN |
| `scripts/test_v2_keys.py::test_azure_key` | Function exists; historical signature/cause UNKNOWN |
| `tests/test_config_api.py::TestIsolation::test_the_call_server_has_no_config_routes` | Assertion exists; historical signature/cause UNKNOWN |
| `tests/test_test_call.py::TestTheProcessSplitSurvives::test_the_call_server_has_no_test_call_routes` | Assertion exists; historical signature/cause UNKNOWN |

Do not explain those failures as invalid keys or mounted config routes without
tracebacks. The inspected `shuo/server.py` mounts V2, not config routes, so the
failure name alone cannot establish the original cause.

For a later authorized baseline, record command/selection, revision/dirty state,
Python/dependency environment, result, exact node ID and sanitized exception/
assertion signature. Compare before/after by identity **and signature**. Equal
failure counts can conceal regressions. Existing failures are not permission to
fix unrelated code; obtain scope for each fix. Preserve historical results as history.

## Bluetooth test layers — PLANNED

- Phase 2: injected discovery/process/provider/clock boundaries; synthetic PCM and
  µ-law fixtures; known vectors, endian/rate/channel rejection, arbitrary chunk
  splits, converter continuity/reset, bounded overflow and idempotent cleanup.
  No device access, process streams or active-call audio.
- Phase 3: separately authorized real PipeWire streams with synthetic signals;
  confirm direction, selection, negotiated contract, process failure/exit and
  device disappearance before attaching the live SHUO pipeline.
- Phase 4: injected conversation integration; no actual provider traffic without
  additional authorization. Preserve carrier/browser tests and completion rules.
- Phase 5: separately authorized controlled cellular E2E, digital duplex,
  audio integrity, STT self-audio/echo checks, latency and manual abort.
- Phase 6: authorized answer/hangup/remote-disconnect/cancellation tests; complete
  ownership across partial start, duplicate events and bounded shutdown.
- Phase 7: disconnect/reconnect, stale audio, concurrency, coexistence, provider
  failure, overload, resource leak, security/privacy and full regression checks.
- Phase 8: operator configuration, supported installation matrix, diagnostics,
  packaged startup, feature-off operation and release/rollback rehearsal.

## Measurements and completion evidence

Measure capture-to-STT, EOT-to-first-token, token-to-first-TTS, conversion/queue
residence, first-uplink-sample and caller mouth-to-ear independently. Use monotonic
clock timestamps for local durations and synchronized/correlated approved signals
for cross-device latency; do not subtract unsynchronized wall clocks. Record
warm/cold runs and distributions. Bluetooth thresholds, sample size, duration,
queue budgets and echo acceptance are **TBD pending approval before the gate**.

Use distinguishable uplink/downlink signals, compare duration and sample counts,
check clipping/discontinuities, and show outbound-only signals do not reach STT.
For cleanup, compare task/process/handle counts before/after start, stop, partial
failure and repeated cycles; for reconnect, verify fresh contract and empty queues.
For concurrency, prove session isolation and preserve carrier player timing under
approved simultaneous load. Existing player tests have their own timing assertions;
these do not establish Bluetooth latency.

A phase-completion record needs scoped authorization, revision, commands, fixtures,
environment/capability versions, gate results, failure identities/signatures,
sanitized artifacts, known limitations, rollback result and explicit approval to
advance. Phase 1 is the special supplied-runtime record, not re-run here.
