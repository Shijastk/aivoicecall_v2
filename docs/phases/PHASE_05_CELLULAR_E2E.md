# Bluetooth Phase 5 — Controlled cellular end-to-end validation

**Status: GATE 1 LIVE RUN APPROVED / PHASE NOT ACCEPTED.**

Shared contracts: [Roadmap](../ROADMAP.md), [requirements](../REQUIREMENTS.md),
[Bluetooth architecture](../BLUETOOTH_ARCHITECTURE.md),
[current architecture](../CURRENT_ARCHITECTURE.md), [testing](../TESTING.md).
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

This evidence authorizes proceeding to the approved Gate 1 live run only. It is not Phase 5 acceptance.

## Files and boundaries

Opt-in Bluetooth integration harness and synthetic/approved measurement tooling; existing tracer/monitor observation points must be reviewed for sufficient timing provenance before making latency claims.

## Tests and measurable acceptance gate

Capture both directions with approved non-private/content-free signals; demonstrate no outbound-only AI audio submitted to STT, no acoustic laptop route and intelligible bidirectional conversation. Measure capture-to-STT, EOT-to-first-token/TTS and caller mouth-to-ear only with valid clocks/correlation. Report warm/cold distributions against separately approved thresholds; manual abort and cleanup must pass. A successful transcript alone is insufficient.

Gate 1 does not invent missing quantitative latency/echo thresholds. Any final Phase 5 acceptance claim still requires an approved measurement methodology, sample size and thresholds.

## Risks

Phone/network echo despite isolated software; unmeasured resampler delay; acoustic route mistaken for digital success; first integration lacks automated call control; known STT recognition limitations may reduce conversational accuracy during this gate.

## Rollback

Use confirmed manual phone hangup, stop optional pipeline and both streams, restore prior routes, remove transient artifacts per approved retention. Feature remains off; do not redial automatically.

## Artifacts and documentation to update

Authorized sanitized E2E report, latency/echo methodology and results, compatibility audio/E2E status, defects and phase gate evidence. Do not commit raw audio or private transcript content.

## Approval and stop boundary

Gate 1 live validation is explicitly approved within the scope above. Explicit Phase 6 approval is still required for call-control capability discovery/integration or commands; no D-Bus action is inferred from Phase 5 success.

Stop after the authorized Gate 1 run, report evidence and unresolved gates. Do not
mark Phase 5 accepted, mark a later phase implemented, or start later-phase work from this document alone.
