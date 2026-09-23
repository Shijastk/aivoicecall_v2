# Bluetooth Phase 5 — Controlled cellular end-to-end validation

**Status: GATE 1 NON-LIVE PREPARATION COMPLETE / LIVE MANUAL EVIDENCE PENDING / PHASE NOT ACCEPTED.**

Shared contracts: [Roadmap](../ROADMAP.md), [requirements](../REQUIREMENTS.md),
[Bluetooth architecture](../BLUETOOTH_ARCHITECTURE.md),
[current architecture](../CURRENT_ARCHITECTURE.md), [testing](../TESTING.md).
The controlled manual procedure is pinned in
[PHASE_05_GATE1_RUNBOOK](PHASE_05_GATE1_RUNBOOK.md).
Future phase scope and gates remain subject to explicit review; file names below are
candidate locations unless explicitly identified as existing code.

## Goal and included scope

Validate the digital caller → SHUO → caller loop under an explicitly controlled real cellular call.

Authorized calls with validated reference capabilities; digital duplex, STT/LLM/streaming TTS, interruption, first response and prolonged conversation; latency distributions, audio integrity/self-audio/echo checks and manual abort.

## Explicit exclusions

Unattended operation, automatic call-control release, broad compatibility claims, arbitrary callers and acoustic speaker/mic bridging.

## Dependencies and prerequisites

The original Phase 5 contract required Phase 4 accepted before Phase 5 execution, plus separate live-call/provider/device authorization; approved participants, recording/retention scope and run duration; confirmed manual phone hangup and bounded media shutdown; and approved latency/echo thresholds and measurement sample size before final acceptance.

That prerequisite history is preserved. On 2026-09-15 the task owner explicitly approved only **Phase 5 Gate 1** as a controlled exception while postponing the unresolved STT-accuracy investigation. This approval does **not** retroactively mark Phase 4 fully accepted and does not waive any architecture or safety invariant.

## Gate 1 owner authorization — 2026-09-15

Approved scope for this controlled reference-user run:

- reference device: itel P40+
- participants: task owner + authorized second-phone/test caller only
- providers: current production path (Deepgram Flux + Groq + ElevenLabs)
- maximum run duration: 5 minutes
- target sample: 10 conversational turns
- Flux final EOT threshold: 0.8 for this controlled reference-user run
- raw audio recording: **not approved**
- diagnostics: opt-in content-free `--diagnose-caller-audio`
- transcript-bearing trace may remain local under `/tmp/shuo` for the controlled run; only sanitized metrics/evidence may be committed or posted publicly
- answer and hangup remain manual; Phase 6 call-control commands are out of scope
- STT accuracy remains a known postponed limitation
- caller mouth-to-ear `<500 ms` must not be claimed from current local tracing alone; a valid external clock/correlation method is still required before that acceptance claim

Gate 1 checks are limited to intelligible digital duplex conversation, no AI-output/self-audio submitted to STT, no laptop acoustic fallback route, interruption/barge-in behavior, and clean bounded teardown.

Repository governance remains fully in force: `AGENTS.md`, `CLAUDE.md`, `rules.md`, `docs/README.md`, architecture, requirements, testing, roadmap, decisions, known issues and the owning phase documents must be followed before any implementation change. No dependency addition, secret inspection, provider/audio-contract change, undocumented scope expansion, Phase 6 work or broad compatibility/performance claim is authorized by Gate 1.

## Pre-live automated evidence — 2026-09-15

Reference revision before the live Gate 1 run:

```text
91d07a8dc1fe69b75d15063d5599c3dad087ec22
```

Executed by the task owner:

- Bluetooth-focused regression: **162 passed, 3 warnings**
- full repository regression: **989 passed, 4 failed, 4 warnings**
- all four full-suite failures match the documented historical baseline identities/signatures
- no new Bluetooth or Phase-5-preflight regression identity was observed

The later Gate 1 preparation commits are documentation-only; they do not change production/test behavior. Current source inspection found no evidence that a new runtime change is justified before the controlled live run. Existing discovery, directional media isolation, route isolation/restoration, diagnostics, lifecycle logging and manual runner already provide the required Gate 1 seams. Adding speculative behavior before a measured live failure would violate the repository's measure-before-change rule.

This evidence authorizes proceeding to the approved Gate 1 live run only. It is not Phase 5 acceptance.

## Files and boundaries

Opt-in Bluetooth integration harness and synthetic/approved measurement tooling; existing tracer/monitor observation points must be reviewed for sufficient timing provenance before making latency claims.

The current local tracer is transcript-bearing and uses process-local relative timing. It may remain a private debugging artifact under the approved retention scope, but it is not a valid caller mouth-to-ear clock/correlation source. Gate 1 therefore uses content-free diagnostics for public/sanitized evidence and leaves caller mouth-to-ear acceptance pending a separately valid methodology.

## Tests and measurable acceptance gate

Capture both directions with approved non-private/content-free signals; demonstrate no outbound-only AI audio submitted to STT, no acoustic laptop route and intelligible bidirectional conversation. Measure capture-to-STT, EOT-to-first-token/TTS and caller mouth-to-ear only with valid clocks/correlation. Report warm/cold distributions against separately approved thresholds; manual abort and cleanup must pass. A successful transcript alone is insufficient.

Gate 1 does not invent missing quantitative latency/echo thresholds. Any final Phase 5 acceptance claim still requires an approved measurement methodology, sample size and thresholds.

## Risks

Phone/network echo despite isolated software; unmeasured resampler delay; acoustic route mistaken for digital success; first integration lacks automated call control; known STT recognition limitations may reduce conversational accuracy during this gate.

## Rollback

Use confirmed manual phone hangup, stop optional pipeline and both streams, restore prior routes, remove transient artifacts per approved retention. Feature remains off; do not redial automatically.

## Artifacts and documentation to update

Authorized sanitized E2E report, latency/echo methodology and results, compatibility audio/E2E status, defects and phase gate evidence. Do not commit raw audio or private transcript content.

The manual procedure, abort criteria, post-run cleanup checks and evidence-handling rules are owned by [PHASE_05_GATE1_RUNBOOK](PHASE_05_GATE1_RUNBOOK.md). After the live run, update the owning phase, `ROADMAP`, `TESTING`, `COMPATIBILITY`, `KNOWN_ISSUES`, `DECISIONS` and `context.md` with observed evidence; do not pre-write PASS results.

## Approval and stop boundary

Gate 1 live validation is explicitly approved within the scope above. Explicit Phase 6 approval is still required for call-control capability discovery/integration or commands; no D-Bus action is inferred from Phase 5 success.

Stop after the authorized Gate 1 run, report evidence and unresolved gates. Do not
mark Phase 5 accepted, mark a later phase implemented, or start later-phase work from this document alone.

## TTS cost/resilience owner amendment — 2026-09-16

The task owner later approved a scoped TTS implementation change after repeated ElevenLabs test usage incurred non-trivial cost. This is a later owner decision and supersedes only the earlier Gate 1 provider/dependency prohibition for the TTS scope described here; it does not broaden the call, recording, privacy, Phase 6 or acceptance scope.

Approved behavior:

- ElevenLabs remains SHUO's default and intended production-quality primary TTS.
- `TTS_PROVIDER=espeak` may be used for cost-free functional testing and must not contact ElevenLabs.
- `TTS_PROVIDER=elevenlabs` plus `TTS_FALLBACK_PROVIDER=espeak` enables an opt-in emergency local fallback.
- If the ElevenLabs key is absent while that fallback is configured, SHUO may select eSpeak without contacting ElevenLabs.
- If ElevenLabs fails before producing its first audio chunk, only bounded text not yet heard by the caller may be replayed through eSpeak.
- Once ElevenLabs has emitted any audio, same-turn replay in eSpeak is forbidden to avoid duplicated speech.
- eSpeak PCM exists only inside the provider implementation and is converted in memory to the existing mono G.711 mu-law/8 kHz boundary before the player; the carrier/core audio contract remains unchanged.
- No raw audio file is written.
- `espeak-ng` is an optional system dependency authorized for this scoped provider/fallback behavior; it is not added to Python requirements.

Detailed provider behavior, installation and regression commands are owned by [TTS_PROVIDERS](../TTS_PROVIDERS.md).

For Phase 5 interpretation, eSpeak-backed live evidence may establish only provider-independent functional observations (digital duplex/routing, turn-taking, interruption mechanics, continuity, manual hangup and bounded cleanup). It cannot establish ElevenLabs TTS latency/quality or caller mouth-to-ear performance. Existing ElevenLabs runs remain separate evidence. Phase 5 remains **not accepted** until all remaining Gate 1 observations are reviewed and the final quantitative latency/echo methodology, sample and thresholds required by this document are separately approved and satisfied.
## Owner-authorized supplemental Android ADB synthetic-caller TX — 2026-09-23

The owner authorized a Vobiz-free supplemental synthetic-caller transmit path
after direct reference-device experiments. On itel P683L / Android 13, shell UID
2000 exposed a unique `TYPE_TELEPHONY` output; during an active manually
controlled cellular call, `AudioTrack.getRoutedDevice()` returned telephony
type 18. Generated tone, Pocket speech and live ADB-stdin PCM were then heard
clearly at the remote Galaxy A10.

The repository implementation is deliberately outside default production
entrypoints and preserves all locked media/privacy boundaries: no raw-audio
persistence, no automatic call control, no Vobiz dependency for this supplemental
path, no shared/carrier PCM contract change, and no caller-heard latency claim.

A local timing probe observed Pocket-ready at 531.8 ms and first PCM at 630.2 ms
from that probe process start. The difference is component-local evidence only.
Phase 5 remains not accepted by this supplemental result. Reverse/downlink capture
is separately unvalidated, and Phase 6 remains outside this implementation.
## Supplemental cellular downlink evidence and receive candidate — 2026-09-23

The owner completed the previously missing reference-phone capability check with
upstream scrcpy 4.1. On the itel P683L during `MODE_IN_CALL`,
`voice-call-downlink --require-audio` delivered Galaxy A10 speech clearly to
Ubuntu. With Ubuntu monitoring on headphones the result was clear with no echo.

This is sufficient to authorize a bounded SHUO-owned receive candidate using the
same Android `VOICE_DOWNLINK` direct-capture concept and proven
PCM16/48 kHz/stereo shape. It is not sufficient to pre-write a PASS for the new
helper. The candidate must separately demonstrate its own `STREAM_READY` and
non-silent content-free capture metrics on the reference call.

All existing Phase-5 restrictions remain: manual answer/hangup, no raw-audio
persistence, content-free public diagnostics, no Phase 6, and no caller-heard
latency claim from local timestamps.
## SHUO-owned Android RX reference evidence — 2026-09-23

The repository-owned receive bridge has now passed the separate live gate that
was intentionally left open after the scrcpy capability test.

On the itel P683L during a manually answered real cellular call, the bounded
8-second probe reported 1,519,616 PCM bytes, 371 chunks, peak RMS 4300, average
RMS 1541.0, and 63,318 converted SHUO mu-law bytes. It explicitly reported no
raw-audio persistence and no caller-content logging.

This establishes the caller-side cellular downlink transport boundary on the
reference device. Together with the already-proven ADB Telephony-Tx path, both
synthetic-caller media directions are now available for Phase-5 development
tooling. Manual call control, no raw recording, content-free public diagnostics,
no caller-heard latency claim, Phase-5-not-accepted status and the Phase-6 block
all remain unchanged.
## SHUO-owned Android RX reference evidence — 2026-09-23

The repository-owned receive bridge has now passed the separate live gate that
was intentionally left open after the scrcpy capability test.

On the itel P683L during a manually answered real cellular call, the bounded
8-second probe reported 1,519,616 PCM bytes, 371 chunks, peak RMS 4300, average
RMS 1541.0, and 63,318 converted SHUO mu-law bytes. It explicitly reported no
raw-audio persistence and no caller-content logging.

This establishes the caller-side cellular downlink transport boundary on the
reference device. Together with the already-proven ADB Telephony-Tx path, both
synthetic-caller media directions are now available for Phase-5 development
tooling. Manual call control, no raw recording, content-free public diagnostics,
no caller-heard latency claim, Phase-5-not-accepted status and the Phase-6 block
all remain unchanged.
