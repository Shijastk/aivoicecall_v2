# SHUO / aivoicecall_v2 Operator Runbook

This file is the day-to-day command entrypoint for development, testing,
Bluetooth validation, evidence capture and phase advancement.

It is intentionally subordinate to:

- `AGENTS.md`
- `CLAUDE.md`
- `rules.md`
- `docs/README.md`
- `docs/REQUIREMENTS.md`
- `docs/TESTING.md`
- `docs/DECISIONS.md`
- `docs/ROADMAP.md`
- the currently authorized phase document

Do not use this runbook to bypass phase authorization. Phase 5–8 remain gated by
their phase documents until explicitly approved and implemented.

## 1. First command in every work session

From repository root:

```bash
./scripts/dev/00_doctor.sh
```

This checks:

- repository root
- Python and virtual environment
- Git branch/HEAD/worktree state
- required project files
- current phase status excerpts
- command availability
- no secret values are printed

Then capture the current repository snapshot:

```bash
./scripts/dev/02_repo_snapshot.sh
```

## 2. Environment setup

Create/recreate a Python virtual environment only when needed:

```bash
./scripts/dev/01_setup_venv.sh
source .venv/bin/activate
```

The setup script uses `python3`, not `python`, because Ubuntu installations may
not provide a `python` alias.

Install dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Do not print or commit `.env`.

If runtime provider access is intentionally required, load your environment in
the current shell yourself:

```bash
set -a
source .env
set +a
```

Verify only whether required variable names are present, without printing values:

```bash
./scripts/dev/00_doctor.sh --runtime-env
```

## 3. Standard test ladder

Always run narrow tests before broad tests.

Core/state/carrier regression:

```bash
./scripts/dev/03_test_core.sh
```

Bluetooth regression:

```bash
./scripts/dev/04_test_bluetooth.sh
```

Current Phase 4B focused gate:

```bash
./scripts/dev/06_test_phase4b.sh
```

Full root regression:

```bash
./scripts/dev/05_test_full.sh
```

Important: the repository historically records four known full-suite failure
identities. Compare failures by **test identity and failure signature**, never by
count alone. See `docs/TESTING.md`.

## 4. Before editing code

Read the governing material:

```bash
sed -n '1,220p' AGENTS.md
sed -n '1,260p' docs/README.md
sed -n '1,260p' docs/REQUIREMENTS.md
sed -n '1,260p' docs/ROADMAP.md
sed -n '1,260p' docs/TESTING.md
sed -n '1,260p' docs/DECISIONS.md
```

For Phase 4:

```bash
sed -n '1,280p' docs/phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md
sed -n '1,260p' docs/phases/PHASE_04_SHUO_PIPELINE_INTEGRATION.md
```

For later phases, read only the phase currently authorized:

```bash
sed -n '1,260p' docs/phases/PHASE_05_CELLULAR_E2E.md
sed -n '1,260p' docs/phases/PHASE_06_CALL_CONTROL_AND_LIFECYCLE.md
sed -n '1,260p' docs/phases/PHASE_07_RESILIENCE_AND_COEXISTENCE.md
sed -n '1,260p' docs/phases/PHASE_08_RELEASE_AND_OPERATIONS.md
```

## 5. Git safety

Before any change:

```bash
git status --short
git log --oneline -5
git branch --show-current
git rev-parse HEAD
```

Never silently reset, clean, stash or overwrite a dirty worktree.

Inspect the current change set:

```bash
git diff --stat
git diff
```

Run the pre-commit verification helper:

```bash
./scripts/dev/10_precommit_verify.sh
```

The helper does not commit or push.

When the current task explicitly authorizes commit/push:

```bash
git add <explicit files>
git diff --cached --stat
git diff --cached
git commit -m "<scoped message>"
git push origin <branch>
```

## 6. Current Bluetooth / Phase 4 workflow

### Offline Phase 4B

```bash
./scripts/dev/06_test_phase4b.sh
```

### Inspect PipeWire/BlueZ call nodes

This is diagnostic only and does not answer/originate/hang up a call:

```bash
./scripts/dev/07_bluetooth_graph.sh
```

HFP call nodes may exist only while a cellular call is already active.

### Controlled Phase 4B shadow run

Only when the current task explicitly authorizes a live device/provider/call run.

Set non-secret runtime parameters:

```bash
export BT_ADDRESS="00:C7:11:7B:84:21"
export PW_LATENCY="120ms"
export LLM_MODEL="qwen/qwen3.6-27b"
export LIVE_CALL_AUTHORIZED="YES"
```

Load provider credentials yourself:

```bash
set -a
source .env
set +a
```

Start/answer the cellular call manually first, then:

```bash
./scripts/dev/08_run_bt_shadow.sh
```

Filter sanitized timing evidence:

```bash
./scripts/dev/09_filter_bt_shadow_log.sh
```

The runner must never be interpreted as permission to answer/originate/hang up
the phone automatically.

## 7. Phase 4 current gate

Current recorded evidence says:

- Phase 4A measurement completed on the reference path.
- Phase 4B shadow mechanism passed offline regression and controlled live validation.
- The current eager trigger produced 0 `ready_before_final` results in 10 final turns.
- Phase 4C prepared-response reuse is therefore **not yet justified**.
- The measured TTS 8-second warm-pool churn and an earlier safe speculative trigger
  should be addressed before revisiting Phase 4C.

Check the live repository source of truth instead of trusting this summary:

```bash
./scripts/dev/12_phase_status.sh
```

## 8. TTS warm-pool latency work

Before changing `TTSPool`, inspect:

```bash
grep -R "class TTSPool\|ttl=" -n shuo tests | head -100
sed -n '1,320p' shuo/services/tts_pool.py
grep -R "TTSPool" -n tests shuo | head -120
```

After implementing a separately reviewed TTS warm/liveness change:

```bash
python -m pytest -q \
  tests/test_tts_failure.py \
  tests/test_bluetooth_production.py \
  tests/test_bluetooth_conversation.py \
  -p no:cacheprovider

./scripts/dev/04_test_bluetooth.sh
./scripts/dev/05_test_full.sh
```

Do not claim the TTS latency issue is fixed until an authorized runtime run records
the before/after setup and first-audio distributions.

## 9. Phase 5 — controlled cellular E2E

Current repository status: planned / pending approval.

Prerequisites from the phase document include:

- Phase 4 accepted.
- Explicit provider/device/live-call authorization.
- Approved participants and recording/retention scope.
- Manual abort/hangup verified.
- Latency/echo thresholds and sample size approved before testing.

The operator toolkit intentionally does not invent a Phase 5 live command before
the implementation/harness exists.

Use:

```bash
./scripts/dev/13_phase_gate.sh 5
```

When Phase 5 implementation lands, add its exact offline and live commands to this
runbook and `docs/TESTING.md` in the same task.

Required evidence includes capture-to-STT, EOT-to-first-token/TTS,
caller mouth-to-ear, audio integrity/self-audio/echo, manual abort and cleanup.

## 10. Phase 6 — call control/lifecycle

Current status: planned / pending approval.

Use:

```bash
./scripts/dev/13_phase_gate.sh 6
```

Do not assume BlueZ/PipeWire audio nodes provide answer/hangup. The reference
environment historically exposed D-Bus call-control interfaces, but automated SHUO
control still requires explicit Phase 6 implementation and validation.

Gate must cover:

- local answer/hangup
- remote disconnect
- cancellation at every startup boundary
- duplicate/late/out-of-order events
- process failure/timeout
- deterministic idempotent cleanup
- no stale audio/double-final history

## 11. Phase 7 — resilience/coexistence/load

Use:

```bash
./scripts/dev/13_phase_gate.sh 7
```

This phase owns reconnect/stale-audio/concurrency/coexistence/overload/resource
leak/security/privacy qualification. Do not move its load acceptance into an
earlier phase merely because a semaphore or bounded queue already exists.

## 12. Phase 8 — release/operations

Use:

```bash
./scripts/dev/13_phase_gate.sh 8
```

This phase should own the supported install/start/diagnostic/rollback experience.
Do not treat development-quality code as a deployed release before this gate.

## 13. Evidence capture

Create a sanitized local evidence folder:

```bash
./scripts/dev/11_evidence_bundle.sh
```

It records command-safe information such as:

- Git HEAD/branch/status
- Python version
- selected public test outputs if you copy them into the evidence directory
- phase status excerpts

It does not copy `.env` or secrets.

Recommended naming:

```text
evidence/
  YYYYMMDD-HHMMSS/
    git.txt
    phase-status.txt
    notes.md
    test-*.txt
    live-*.log
```

## 14. Updating docs after a change

Use the ownership contract from `docs/README.md`:

- behavior/interface: `PROJECT.md`, `CURRENT_ARCHITECTURE.md`
- requirements/scope: `REQUIREMENTS.md`
- hardware/formats: `BLUETOOTH_ARCHITECTURE.md`, `COMPATIBILITY.md`
- tests/results: `TESTING.md`
- limitations: `KNOWN_ISSUES.md`
- rationale: `DECISIONS.md`
- phase status: `ROADMAP.md` + specific phase doc
- milestone: append to `context.md`

Do not rewrite historical evidence to make new behavior look compliant.

## 15. Common recovery commands

Venv activation:

```bash
source .venv/bin/activate
```

If the venv does not exist:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Check Python:

```bash
python --version
python3 --version
which python
```

Check Git remote/auth:

```bash
git remote -v
git branch --show-current
git status
```

Do not put access tokens directly into commands or shell history.

Check Bluetooth/PipeWire tools:

```bash
command -v pw-dump
command -v pw-cat
command -v pactl
command -v bluetoothctl
```

Inspect owned pw-cat processes after teardown:

```bash
pgrep -a pw-cat || true
```

Expected after clean teardown: no project-owned `pw-cat` process remains.

## 16. Golden workflow for every future phase

```text
1. Read AGENTS + source-of-truth docs.
2. Confirm exact Git HEAD and worktree state.
3. Inspect actual code/tests.
4. Define the current phase gate.
5. Make only the scoped change.
6. Run focused offline tests.
7. Run broader regression.
8. If separately authorized, run provider/device/live-call validation.
9. Record sanitized evidence.
10. Update owning docs in the same task.
11. Review git diff.
12. Commit/push only when explicitly authorized.
13. Advance to the next phase only when the preceding gate is actually met.
```
