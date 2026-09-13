# Repository instructions

Read [docs/README.md](docs/README.md), then the relevant architecture,
requirements, testing, decisions and phase document before planning or changing code.
Read [CLAUDE.md](CLAUDE.md) and [rules.md](rules.md) for their existing engineering
restrictions; this file does not remove or weaken them. Verify their historical
implementation claims against source (see docs/KNOWN_ISSUES.md).

- Inspect actual code and tests before making implementation claims. Cite paths
  and symbols; distinguish code evidence, runtime evidence, plans and unknowns.
  Report code/documentation or instruction conflicts; do not silently resolve
  them or edit documentation merely to make an implementation look compliant.
- Preserve low-latency carrier streaming, the pure `process_event` state machine,
  provider boundaries, and existing Vobiz, Twilio and browser/V2 behavior.
  Preserve configuration/process separation, recording, monitoring, spool,
  notifications and call history. Avoid unrelated rewrites and refactoring.
- Bluetooth is additive and isolated. Future device code must not be imported
  by the production server by default. Preserve the carrier µ-law 8 kHz contract;
  proposed Bluetooth PCM conversion belongs only at its separate boundary.
- Never hard-code transient numeric PipeWire IDs. The reference phone model,
  Bluetooth address and node names are fixture data, never the only supported
  device. Select by validated capabilities; require explicit selection if ambiguous.
- Separate Bluetooth caller downlink and phone uplink structurally. Never feed
  AI/TTS outbound audio into STT or silently fall back to a default mic/speaker.
- New realtime code must use bounded queues, explicit overflow policies and
  deterministic cleanup. Inject hardware, process and provider boundaries for
  testing. Existing gaps are not permission for unrelated fixes.
- Do not read, expose or modify secrets, credentials, tokens or `.env` files.
  Do not inspect private call contents or customer data without explicit scope.
  Do not add dependencies without explicit approval.
- Preserve unrelated changes in a dirty worktree. Do not silently fix unrelated
  test failures. Run focused tests before full regression when authorized;
  compare failures by test identity and signature, never by count alone.
- Update affected docs when architecture, behavior, public interfaces,
  dependencies, supported hardware, audio contracts, tests/commands/baselines,
  known issues, roadmap/phase status or decisions change. Follow the ownership
  and evidence rules in docs/README.md. Preserve historical evidence.
- Do not commit, push, control real calls, access live devices or start a later
  phase unless explicitly authorized by the current task. Planning is not
  approval to implement. Stop at the requested phase boundary and report results.
