# Phase 5 Gate 1 — controlled manual runbook

**Status: NON-LIVE PREPARATION COMPLETE / MANUAL LIVE EVIDENCE PENDING.**

This runbook is the executable checklist for the task-owner-approved Phase 5 Gate 1 cellular validation. It does not replace `PHASE_05_CELLULAR_E2E.md`, relax any repository restriction, or authorize Phase 6.

## Governing documents

Before changing implementation or interpreting evidence, follow:

- `AGENTS.md`
- `CLAUDE.md`
- `rules.md`
- `docs/README.md`
- `docs/REQUIREMENTS.md`
- `docs/TESTING.md`
- `docs/ROADMAP.md`
- `docs/DECISIONS.md`
- `docs/KNOWN_ISSUES.md`
- `docs/BLUETOOTH_ARCHITECTURE.md`
- `docs/CURRENT_ARCHITECTURE.md`
- `docs/phases/PHASE_05_CELLULAR_E2E.md`
- GitHub Issue #3 and its latest owner authorization

Source code wins over stale descriptive documentation when they differ, but source evidence never grants permission to weaken a restriction.

## Locked Gate 1 authorization

The approved live scope is intentionally narrow:

- reference phone: itel P40+
- participants: task owner + authorized second-phone/test caller only
- current provider path only: Deepgram Flux + Groq + ElevenLabs
- maximum live duration: 5 minutes
- target: 10 conversational turns
- explicit Flux final EOT threshold: `0.8`
- raw audio recording: **not approved**
- diagnostics: content-free `--diagnose-caller-audio`
- transcript-bearing trace: local `/tmp/shuo` only; do not publish it
- answer/hangup: manual only
- Phase 6 D-Bus/call-control integration: out of scope
- STT-accuracy research: postponed; known limitation remains open
- no caller-heard `<500 ms` claim from local SHUO timestamps alone

No dependency addition, provider swap, codec/core contract change, secret inspection, broad compatibility claim, or automatic call control is authorized.

## Automated pre-live evidence already completed

At source revision `91d07a8dc1fe69b75d15063d5599c3dad087ec22`, before the documentation-only Gate 1 authorization commits, the task owner executed:

- Bluetooth-focused suite: **162 passed, 3 warnings**
- full repository suite: **989 passed, 4 failed, 4 warnings**

The four failures matched the documented baseline identities and signatures exactly:

- `scripts/test_v2_keys.py::test_shunya_key`
- `scripts/test_v2_keys.py::test_azure_key`
- `tests/test_config_api.py::TestIsolation::test_the_call_server_has_no_config_routes`
- `tests/test_test_call.py::TestTheProcessSplitSurvives::test_the_call_server_has_no_test_call_routes`

No new Bluetooth/Phase-5-preflight failure identity was observed. The later Phase 5 documentation commits do not alter production or test code.

## Why no additional production code is added before the live run

Current source already provides the Gate 1 seams needed for a controlled live test:

- capability-based Bluetooth target discovery and explicit `pw-cat` targeting
- structurally separate caller capture and AI playback paths
- fail-closed removal of physical ALSA mic/speaker links for AI-only mode
- bounded teardown/restoration of owned routes and `pw-cat` processes
- content-free selected-node, route, audio-energy and Flux-event diagnostics
- content-free lifecycle/cancellation and local stage timing logs
- manual runner with explicit EOT threshold and no automatic answer/hangup

The current tracer is transcript-bearing and its timestamps are local-process relative. It is useful for private debugging but is **not** valid proof of caller mouth-to-ear latency. Adding speculative production code before a live Gate 1 failure would violate the measure-before-change rule. Therefore the correct pre-live implementation decision is **no new runtime behavior**.

## Morning live procedure

### 1. Synchronize without overwriting local artifacts

```bash
cd ~/Projects/aivoicecall_v2
source .venv/bin/activate

git pull --ff-only origin main
git status --short
```

Existing untracked local audio/log artifacts are not a failure and must not be deleted merely to make the tree look clean.

### 2. Load existing local environment without inspecting or printing secrets

```bash
set -a
source .env
set +a
mkdir -p /tmp/shuo
```

Do not `cat`, print, commit or paste `.env` values.

### 3. Establish the cellular call manually

Using the authorized second phone, call the itel P40+ and answer manually. Wait until the HFP call is active before starting SHUO. Do not use D-Bus answer/hangup commands in this phase.

### 4. Start the approved Gate 1 runner

```bash
PYTHONPATH=. \
.venv/bin/python scripts/run_bluetooth_ai.py \
  --bluetooth-address 00:C7:11:7B:84:21 \
  --latency 120ms \
  --eot-threshold 0.8 \
  --diagnose-caller-audio \
  --call-id phase5-gate1 \
  2>&1 | tee /tmp/shuo/phase5-gate1-console.log
```

Do not add Phase 4 speculative/4D tuning flags to this reference Gate 1 run unless separately approved; this gate validates the current production path with only the already-approved EOT `0.8` reference-user setting and diagnostics.

### 5. Conversation sample

Stay below 5 minutes and aim for 10 caller turns.

Use ordinary, non-private conversation. Avoid using the test to re-open the postponed technical-proper-noun STT investigation. Include:

1. at least six normal caller → AI exchanges;
2. one deliberate thinking pause inside a question to confirm the `0.8` reference setting does not immediately repeat the old premature-answer behavior;
3. at least two genuine barge-ins while the AI is audibly speaking; after interrupting, continue with a new complete sentence;
4. at least one later follow-up that depends on an earlier turn, to exercise short-session continuity.

Do not continue the call merely to reach 10 if any abort condition below appears.

## Gate 1 observations to record manually

Record only pass/fail/short non-private notes for these items:

| Check | Required observation |
|---|---|
| Digital duplex | Caller speech reaches SHUO and AI speech is heard by the remote caller |
| No acoustic fallback | Laptop physical speaker is not being used as the call output and laptop physical mic is not the AI call input |
| No obvious AI self-audio into STT | AI speech alone does not create a new caller turn/response loop; phone/network echo is still a separate risk |
| Turn-taking | Thinking pause does not reproduce the previously observed immediate premature answer in this reference run |
| Barge-in #1 | AI output is interrupted and the new caller turn is processed |
| Barge-in #2 | Same behavior is independently observed a second time |
| Continuity | Later follow-up remains conversationally connected, subject to the known postponed STT limitation |
| Manual abort/hangup | Phone can be hung up manually without needing automated control |
| Bounded cleanup | Runner stops/returns, owned routes restore or become obsolete only because the BlueZ call ports disappeared, and no `pw-cat` remains |

STT proper-noun errors remain a documented postponed limitation; do not silently classify them as fixed. Conversely, if recognition is so poor that intelligible conversation cannot be sustained, record Gate 1 as failed rather than hiding the effect behind the postponement.

## Abort conditions

Immediately end the controlled call manually and stop the runner if any of the following occurs:

- audio routes to an unintended physical/default device;
- runaway self-response/feedback loop;
- loud/unsafe audio behavior;
- repeated unrecoverable playback/capture failure;
- provider/runtime fatal error that leaves the session in an uncertain state;
- cleanup cannot be bounded;
- the five-minute authorization limit is reached.

If the phone is already hung up and the runner has not exited, use `Ctrl+C`. Do not redial automatically.

## Post-run cleanup checks

After manual hangup and runner exit:

```bash
pgrep -a pw-cat || echo "NO pw-cat processes"

git status --short
```

For public/sanitized review, extract only content-free diagnostic/lifecycle lines instead of pasting the complete console log:

```bash
grep -E 'BTDiagnostic|BTLifecycle|BTLatency|BTStartup|PlaybackClear' \
  /tmp/shuo/phase5-gate1-console.log \
  > /tmp/shuo/phase5-gate1-sanitized.log
```

The full console log and `/tmp/shuo/phase5-gate1.json` may contain private transcript/provider details. Keep them local unless a new explicit scope approves inspection. Never commit raw audio or private transcript content.

## Evidence interpretation rules

A Gate 1 pass may establish only the controlled reference-run behaviors actually observed. It does not establish:

- universal Android/Linux/device compatibility;
- automated answer/hangup/lifecycle ownership;
- reconnect/resilience qualification;
- STT accuracy resolution;
- echo below an invented numeric threshold;
- caller mouth-to-ear `<500 ms`;
- Phase 6 readiness by implication.

Local timestamps can support component timing such as Flux EOT → Agent start and provider/TTS stages, but caller mouth-to-ear requires a separately valid external clock/correlation methodology. No threshold is invented in this runbook.

## After the live run

Only after reviewing the live evidence should the owning Phase 5 document, `ROADMAP.md`, `TESTING.md`, `COMPATIBILITY.md`, `KNOWN_ISSUES.md`, `DECISIONS.md` and `context.md` be updated with sanitized results. Preserve failures and unresolved gates instead of rewriting history. Phase 5 must remain unaccepted if any required acceptance evidence is missing.

Phase 6 remains blocked until Phase 5 evidence is reviewed and the task owner gives separate explicit Phase 6 approval.

## Owner amendment — 2026-09-16: cost-controlled TTS testing and emergency fallback

After the original locked Gate 1 authorization above, the task owner explicitly approved a narrow TTS scope amendment to avoid repeated ElevenLabs charges during heavy testing and to prevent a pre-audio ElevenLabs failure from leaving an AI turn silent.

This later owner decision supersedes **only** the earlier Gate 1 restrictions that said "current provider path only" and "no dependency addition/provider swap". All other Gate 1 restrictions remain unchanged, including the reference phone/participant scope, EOT `0.8`, no raw audio, content-free diagnostics, local-only transcript-bearing artifacts, manual answer/hangup, no Phase 6 and no caller-heard `<500 ms` claim.

The approved implementation was:

- ElevenLabs remained the default/production-quality primary TTS;
- local eSpeak was an explicitly selectable cost-free test provider;
- eSpeak was an opt-in emergency fallback for ElevenLabs failures **before first audio**;
- after any ElevenLabs audio had already been emitted, the current answer was never replayed from the beginning in eSpeak;
- PCM produced by eSpeak was contained inside its provider module and converted to the existing mono mu-law/8 kHz TTS boundary; no PCM/L16 carrier/core route was authorized;
- `espeak-ng` was an optional system dependency, not a new Python runtime dependency;
- no raw audio was written by the provider;
- the state machine and call-control scope were unchanged.

For the remaining functional Gate 1 checks, the then-approved eSpeak command was:

```bash
TTS_PROVIDER=espeak TTS_FALLBACK_PROVIDER= \
PYTHONPATH=. \
.venv/bin/python scripts/run_bluetooth_ai.py \
  --bluetooth-address 00:C7:11:7B:84:21 \
  --latency 120ms \
  --eot-threshold 0.8 \
  --diagnose-caller-audio \
  --call-id phase5-gate1-espeak-final \
  2>&1 | tee /tmp/shuo/phase5-gate1-espeak-final.log
```

eSpeak-backed evidence could support provider-independent functional observations such as digital routing, turn-taking, interruption mechanics, continuity, manual hangup and bounded cleanup. It could **not** support ElevenLabs latency/quality, production voice quality, or caller mouth-to-ear performance. This section remains as historical evidence and is superseded for future functional runs by the later Pocket amendment below.

## Owner amendment — 2026-09-16: Pocket TTS supersedes eSpeak for remaining functional Gate 1 work

After the eSpeak live attempt, transcript-bearing local diagnostics showed one normal AI response for each distinct caller turn and no new STT turn during the supposedly repeated playback. A direct eSpeak synthesis check also completed normally. The task owner concluded that the perceived "loop" came from eSpeak's poor/generic intelligibility rather than evidence of an actual conversation feedback loop.

Before approving another provider change, Pocket TTS and Supertonic 3 were measured outside the repository on the reference MSI laptop. Pocket produced native-stream first audio around 101–105 ms in the three-sentence comparison and then completed 20/20 warm generations with zero failures and 75.1–88.8 ms TTFA (78.3 ms average). In telephone-band 8 kHz G.711 mu-law blind listening, Pocket was preferred in two of three pairs. Supertonic's comparable short-phrase synthesis completion was about 467–490 ms. These are local reference-machine measurements only, not caller-heard latency evidence.

The task owner then explicitly approved replacing the eSpeak runtime testing/fallback implementation with Pocket TTS while preserving every other Gate 1 restriction. The governing provider rules are now:

- ElevenLabs remains the default/production-quality primary provider;
- `TTS_PROVIDER=pocket` is the approved cost-free local functional-test path;
- `TTS_FALLBACK_PROVIDER=pocket` is the optional pre-first-audio ElevenLabs fallback;
- the validated Pocket path uses the built-in catalog voice `alba`, not gated voice cloning;
- native Pocket PCM remains inside the provider boundary and only mono G.711 mu-law/8 kHz reaches SHUO's existing player/core boundary;
- no raw audio is written;
- manual answer/hangup, EOT `0.8`, local-only transcript artifacts, content-free public diagnostics, maximum five-minute call scope and no Phase 6 remain unchanged.

Install the optional local profile before the Pocket run:

```bash
cd ~/Projects/aivoicecall_v2
source .venv/bin/activate
python -m pip install -r requirements-pocket-tts.txt
```

After the final repository automated gate is baseline-clean, the approved remaining **functional** Gate 1 command is:

```bash
TTS_PROVIDER=pocket TTS_FALLBACK_PROVIDER= \
PYTHONPATH=. \
.venv/bin/python scripts/run_bluetooth_ai.py \
  --bluetooth-address 00:C7:11:7B:84:21 \
  --latency 120ms \
  --eot-threshold 0.8 \
  --diagnose-caller-audio \
  --call-id phase5-gate1-pocket-final \
  2>&1 | tee /tmp/shuo/phase5-gate1-pocket-final.log
```

Pocket-backed evidence may support provider-independent functional observations: digital routing, turn-taking, interruption/cancellation mechanics, short-session continuity, manual hangup and bounded cleanup. Because Pocket inference is local CPU work and cancellation is cooperative at the vendor-yield boundary, the reference live run must specifically confirm that barge-in remains prompt while Flux/Groq/Pocket are active together and that no late Pocket audio leaks after interruption.

Pocket evidence must **not** be used to claim ElevenLabs-specific latency/voice quality, production voice quality, caller mouth-to-ear `<500 ms`, or final Phase 5 quantitative latency/echo acceptance. Those remain separate evidence gates. The historical eSpeak amendment above is preserved rather than rewritten; this later Pocket amendment governs future functional Gate 1 runs.
## Supplemental ADB synthetic-caller transmit tool — 2026-09-23

A later owner-authorized development harness can inject synthetic caller speech
from Ubuntu through an itel caller phone's real cellular uplink using USB ADB.
Its owning documentation is
`docs/ANDROID_CELLULAR_SYNTHETIC_CALLER.md`.

This does not rewrite the historical Gate-1 manual command or its acceptance
record. The supplemental tool still requires the cellular call to be established
and answered manually, records no raw audio, performs no automatic hangup, and
does not make local timestamps into caller-heard latency evidence. It is suitable
for repeatable caller stimulus; the reverse caller-phone downlink path is not yet
validated and therefore full closed-loop automation is not claimed.
## Supplemental caller-side receive probe — 2026-09-23

Independent capability evidence now exists for the itel P683L using upstream
scrcpy 4.1 `voice-call-downlink`; clear Galaxy A10 speech reached Ubuntu, and
headphone monitoring eliminated acoustic echo.

The repository-owned receive candidate is intentionally gated separately. During
a manually established/answered authorized cellular call, its bounded
`scripts/dev/17_android_cellular_rx.py probe` command may consume downlink PCM
only in memory and print content-free byte/RMS/conversion metrics. It must not
write a raw-audio file, automate call control, or be interpreted as a latency
measurement. Failure to reach `STREAM_READY` or capture sustained non-silent
data is a stop condition, not a reason to bypass the route/permission checks.
## Supplemental receive probe result — 2026-09-23

The repository-owned caller-side receive probe passed on the reference itel
P683L. The bounded 8-second run captured 1,519,616 PCM bytes over 371 chunks,
with non-zero content-free RMS (peak 4300, average 1541.0), and converted 63,318
bytes to SHUO mu-law in memory. No raw audio or caller content was persisted.

This closes the receive-transport validation item for the reference device. It
does not replace the historical Gate-1 acceptance requirements or authorize
automatic call control.
## Supplemental receive probe result — 2026-09-23

The repository-owned caller-side receive probe passed on the reference itel
P683L. The bounded 8-second run captured 1,519,616 PCM bytes over 371 chunks,
with non-zero content-free RMS (peak 4300, average 1541.0), and converted 63,318
bytes to SHUO mu-law in memory. No raw audio or caller content was persisted.

This closes the receive-transport validation item for the reference device. It
does not replace the historical Gate-1 acceptance requirements or authorize
automatic call control.
## Optional closed-loop synthetic caller run — 2026-09-23

After both Android caller-side media directions were independently qualified,
the owner authorized a deterministic real-cellular caller controller for
supplemental Gate-1 evidence.

The controller is started only after the itel <-> Galaxy cellular call is
manually established/answered and the Galaxy-side SHUO Bluetooth pipeline is
already running. It may:

- keep one caller-side RX and one TX helper alive;
- contact Deepgram for content-private turn observation;
- send prepared Pocket caller utterances;
- exercise a 650 ms thinking pause and two interruption attempts;
- keep response text in memory only for boolean continuity checks.

It may not dial, answer, hang up, save raw audio, serialize response transcripts,
or turn local timings into caller-heard latency. Maximum scenario duration
remains 300 seconds. Any timeout/provider/device error is a failed supplemental
run, not permission to weaken a gate.
### Closed-loop preflight correction after first Galaxy attempt — 2026-09-23

For the Galaxy-side SHUO leg, `bluetoothctl Connected: yes` is not a sufficient
precondition. Before starting `run_bluetooth_ai.py`, verify that the active
cellular call has produced compatible BlueZ SCO PipeWire nodes.

Use the existing content-free graph helper while the call is active:

```bash
cd /tmp/shuo-cellular-loop
./scripts/dev/07_bluetooth_graph.sh
wpctl status
```

Do not continue to the closed-loop controller unless the graph contains the
Galaxy call's BlueZ SCO capture/playback pair. If the SHUO runner reports
`no compatible Bluetooth downlink target`, stop and inspect the live graph;
do not remove the address/profile/codec/format gates or fall back to a default
microphone/speaker.
