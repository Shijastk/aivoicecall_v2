# SHUO documentation index

Snapshot updated 2026-09-13 through Phase 3 base revision
`97076cd1739e2456843ced239eeebeedcfefa70b`, plus documented local Phase 3
closeout changes for AI-only route isolation and capture shutdown cleanup.
Reference-hardware PipeWire discovery, explicit capture/playback, route
isolation/restoration and clean lifecycle teardown have now been executed.
Default production SHUO/call lifecycle integration remains pending.

Instruction audit: no pre-existing AGENTS.md or AGENTS.override.md was found in
the repository, its ancestor directories or the user Codex instruction location.
The new root AGENTS.md preserves existing CLAUDE.md/rules.md restrictions by
reference; both files remain byte-for-byte unchanged.

Documentation correction (task-owner clarification): Phase 1 also validated
manual D-Bus answer/hangup on the reference environment; automated SHUO control
and Phase 6 remain pending. See the corrected [evidence ledger](BLUETOOTH_ARCHITECTURE.md)
and [compatibility matrix](COMPATIBILITY.md). AGENTS.md and the Bluetooth
requirements now explicitly scope the PCM and Linux-only adapter exceptions;
carrier/core protections remain mandatory. [Phase 2](phases/PHASE_02_ADAPTER_AND_CODEC.md)
lists candidate files only; no implementation is authorized or added.

2026-09-16 TTS note: the owner first approved an optional local eSpeak test
provider/pre-audio fallback, then replaced that runtime path with Pocket TTS
after eSpeak proved too unclear for reliable functional testing and Pocket won
the measured local streaming/A-B comparison. ElevenLabs remains the default
production-quality primary. See [TTS_PROVIDERS.md](TTS_PROVIDERS.md) and the
Phase 5 owner amendments. This does not accept Phase 5, authorize Phase 6, or
create a caller-heard latency claim.

## Find the right document

| Question | Source of truth | Evidence type |
|---|---|---|
| What does this backend do? | [PROJECT.md](PROJECT.md) | VERIFIED IN CODE |
| How do calls, audio and processes work? | [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md) | VERIFIED IN CODE |
| How are TTS provider selection and the Pocket local fallback constrained? | [TTS_PROVIDERS.md](TTS_PROVIDERS.md) | IMPLEMENTED contract; reference live validation still required |
| What is the final product? | [PRODUCT_VISION.md](PRODUCT_VISION.md) | PLANNED direction |
| What must be preserved or built? | [REQUIREMENTS.md](REQUIREMENTS.md) | Requirements, not proof of compliance |
| What did hardware validation establish? What is the adapter design? | [BLUETOOTH_ARCHITECTURE.md](BLUETOOTH_ARCHITECTURE.md) | Supplied runtime evidence / PROPOSED design |
| Which systems have compatibility evidence? | [COMPATIBILITY.md](COMPATIBILITY.md) | Reference evidence and validation policy |
| What is next, and what permits advancing? | [ROADMAP.md](ROADMAP.md), [phases/](phases/) | Phase 1 complete; Phase 2 implemented; Phase 3 implemented/reference-validated; later integration phases pending |
| How should changes be tested? | [TESTING.md](TESTING.md) | Code inventory, historical baseline, future gates |
| What is uncertain or problematic? | [KNOWN_ISSUES.md](KNOWN_ISSUES.md) | Recorded failures, source findings, unknowns |
| Why these boundaries? | [DECISIONS.md](DECISIONS.md) | Decisions, evidence and revisit conditions |
| What instructions govern work? | [../AGENTS.md](../AGENTS.md) | Permanent repository guidance |

## Evidence vocabulary

- **VERIFIED IN CODE**: inspected at the revision above; not runtime success.
- **VERIFIED AT RUNTIME**: observed execution with provenance and scope. Here,
  Bluetooth observations are supplied by the task owner, not reproduced.
- **VALIDATED REFERENCE HARDWARE**: only the recorded runtime capability was
  demonstrated on that combination; not a released SHUO integration.
- **PLANNED**: future work pending implementation approval.
- **PROPOSED**: reviewable design or acceptance gate, not yet approved.
- **ASSUMPTION**: hypothesis needing evidence. **UNKNOWN**: evidence absent.
- **OUT OF SCOPE**: excluded from this task or phase; not silently fixed.

A test assertion is evidence of intended coverage, not a passing result.
Provider format requests are not proof of actual returned bytes. Current support
means an implementation exists unless runtime validation is explicitly stated.

## Existing documents and precedence

This index owns navigation for the inspected snapshot and Bluetooth roadmap.
It does not repeal restrictions in `CLAUDE.md` or `rules.md`.
The root [README](../README.md) remains the setup/runbook entrypoint;
[context.md](../context.md) retains historical milestones/decisions.
[plan.md](../plan.md) is legacy carrier/provider research, subject to CLAUDE.md
corrections. [phase8-plan.md](phase8-plan.md) concerns earlier carrier call
management, not Bluetooth Phase 8. [api-plan.md](api-plan.md) is an older API
proposal, not the implemented route contract. The [V2 PRD](../v2%20prd.md) and
[TTS handoff](../handoff-tts-swap.md) retain historical intent/handoff material.

Do not transfer phase numbers, test totals, latency claims or provider plans
from those documents to this Bluetooth roadmap. See KNOWN_ISSUES for drift.
If a claim conflicts, inspect the named code and report the difference;
implementation truth does not authorize changing a restriction or fixing code.

## Maintenance contract

Update the owning document in the same task as the affected implementation:
PROJECT/CURRENT_ARCHITECTURE for behavior/interfaces; REQUIREMENTS for scope;
BLUETOOTH_ARCHITECTURE and COMPATIBILITY for formats/hardware; TESTING and
KNOWN_ISSUES for commands/results; DECISIONS for rationale; ROADMAP and the
specific phase for status. Keep detailed gates in phase documents and summaries
in ROADMAP. Link instead of copying whole sections. Update snapshot/revision
when re-inspecting; attach sanitized evidence and distinguish retained history.
Preserve the existing context.md milestone and append-only decision-log practice.

Never mark a phase complete from code existence alone. Record gate results,
limitations, rollback evidence and approval. No future phase is authorized by
this setup. Report documentation conflicts; do not rewrite requirements after
implementation merely to hide noncompliance.
## 2026-09-23 supplemental Android cellular synthetic-caller TX

The task owner authorized codifying the reference-proven, Vobiz-free Android ADB
cellular transmit path as an isolated Phase-5 development harness. See
[ANDROID_CELLULAR_SYNTHETIC_CALLER](ANDROID_CELLULAR_SYNTHETIC_CALLER.md).

The harness is not imported by production entrypoints, keeps the shared/carrier
audio contract at G.711 mu-law/8 kHz, performs no automatic call control, writes
no raw audio, and does not create a caller-heard latency claim. Its Android edge
is S16LE/16 kHz/mono, matching the already-validated Bluetooth boundary.
### Android caller-side RX follow-up — 2026-09-23

Reference `VOICE_DOWNLINK` capability is now proven on the itel P683L with
upstream scrcpy 4.1. A SHUO-owned no-file receive bridge candidate is under the
same [ANDROID_CELLULAR_SYNTHETIC_CALLER](ANDROID_CELLULAR_SYNTHETIC_CALLER.md)
evidence record and remains gated by its own live reference probe before
qualification.
