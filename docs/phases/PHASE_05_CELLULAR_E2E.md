# Bluetooth Phase 5 — Controlled cellular end-to-end validation

**Status: PLANNED / PENDING APPROVAL.**

Shared contracts: [Roadmap](../ROADMAP.md), [requirements](../REQUIREMENTS.md),
[Bluetooth architecture](../BLUETOOTH_ARCHITECTURE.md),
[current architecture](../CURRENT_ARCHITECTURE.md), [testing](../TESTING.md).
Future phase scope and gates are PROPOSED pending review; file names below are
candidate locations unless explicitly identified as existing code.

## Goal and included scope

Validate the digital caller → SHUO → caller loop under an explicitly controlled real cellular call.

Authorized calls with validated reference capabilities; digital duplex, STT/LLM/streaming TTS, interruption, first response and prolonged conversation; latency distributions, audio integrity/self-audio/echo checks and manual abort.

## Explicit exclusions

Unattended operation, automatic call-control release, broad compatibility claims, arbitrary callers and acoustic speaker/mic bridging.

## Dependencies and prerequisites

Phase 4 accepted. Separate live-call/provider/device authorization; approved participants, recording/retention scope and run duration; confirmed manual phone hangup and bounded media shutdown. Latency/echo thresholds and measurement sample size approved before testing.

## Files and boundaries

Opt-in Bluetooth integration harness and synthetic/approved measurement tooling (PROPOSED); existing tracer/monitor observation points reviewed for sufficient timing provenance.

## Tests and measurable acceptance gate

Capture both directions with approved non-private signals; demonstrate no outbound-only AI audio submitted to STT, no acoustic laptop route and intelligible bidirectional conversation. Measure capture-to-STT, EOT-to-first-token/TTS and caller mouth-to-ear with valid clocks/correlation. Report warm/cold distributions against approved TBD thresholds; manual abort and cleanup pass. A successful transcript alone is insufficient.

## Risks

Phone/network echo despite isolated software; unmeasured resampler delay; acoustic route mistaken for digital success; first integration lacks automated call control.

## Rollback

Use confirmed manual phone hangup, stop optional pipeline and both streams, restore prior routes, remove transient artifacts per approved retention. Feature remains off; do not redial automatically.

## Artifacts and documentation to update

Authorized sanitized E2E report, latency/echo methodology and results, compatibility audio/E2E status, defects and phase gate evidence.

## Approval and stop boundary

Explicit Phase 6 approval for call-control capability discovery and commands; no D-Bus actions inferred from Phase 5 success.

Stop after the authorized phase, report evidence and unresolved gates. Do not
mark a later phase implemented or start its work from this document alone.
