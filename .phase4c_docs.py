from pathlib import Path

RUN_ID = "34966008520"
COMMIT = "bc9996f727bf4ff7870d6f112e73200d82a49b87"


def append_once(path: str, marker: str, block: str) -> None:
    file = Path(path)
    text = file.read_text()
    if marker in text:
        return
    if not text.endswith("\n"):
        text += "\n"
    file.write_text(text + "\n" + block.strip() + "\n")


append_once(
    "docs/phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md",
    "## Phase 4C automated implementation evidence — 2026-09-15",
    f'''
## Phase 4C automated implementation evidence — 2026-09-15

The task owner explicitly authorized Phase 4C implementation while deferring all
new real-call/provider/device validation until repository-level automation is
clean. The implementation is opt-in and does **not** reverse the earlier evidence
that the measured Phase 4B triggers had insufficient live ready-before-final
frequency. It therefore makes no latency-improvement or caller-heard claim.

Implemented contract:

- a speculative provider stream may be retained only after its first content token
  is ready; the remainder stays unread, so no complete response is buffered;
- final `EndOfTurn` may promote that stream exactly once only when the final
  transcript, system prompt and committed history snapshot still match;
- mismatch, `TurnResumed`, timeout, capacity pressure, teardown or invalid history
  discard/cancel the speculative stream and preserve the ordinary final-EOT path;
- promoted tokens still flow through the existing `Agent` token -> TTS -> Player
  callbacks; TTS remains non-speculative;
- the feature is disabled by default and requires the explicit Bluetooth shadow
  path plus `--prepared-response-reuse`; omitting the flag is the rollback;
- carrier/browser/default startup and the pure state machine are unchanged.

Automated verification was executed by GitHub Actions run `{RUN_ID}` on Python
3.12 with no provider secrets, Bluetooth device or cellular call. The run passed:
source application, `py_compile`, the focused Phase 4 regression, the complete
Bluetooth regression, the full root regression compared to the documented four
baseline failures by exact identity/signature, and final `git diff --check`.
The verified implementation was committed as `{COMMIT}`.

Remaining gate: a controlled real call/provider/device exercise is still required
to establish actual latency benefit, audio correctness and caller-heard behavior.
Phase 4C code is repository-verified; Phase 4 overall acceptance is not yet
claimed.
''',
)

append_once(
    "docs/phases/PHASE_04_SHUO_PIPELINE_INTEGRATION.md",
    "## Phase 4C repository-verified implementation — 2026-09-15",
    f'''
## Phase 4C repository-verified implementation — 2026-09-15

Following explicit task-owner authorization, the commit-on-final reuse seam is now
implemented on the Phase 4C feature branch. It is default-off, Bluetooth-only and
fails open to the existing final-EOT Agent generation path. A reusable draft is
limited to an exact-match, first-token-ready provider stream; it never pre-speaks,
never owns TTS before finality and never buffers the complete answer.

GitHub Actions run `{RUN_ID}` passed focused Phase 4, complete Bluetooth and full
baseline-aware regressions plus compile/diff validation. No live provider/device
or real call was used. Real-call validation remains the final gate before any
caller-latency benefit or Phase 4 acceptance claim.
''',
)

append_once(
    "docs/TESTING.md",
    "## Phase 4C prepared-response automated verification — 2026-09-15",
    f'''
## Phase 4C prepared-response automated verification — 2026-09-15

GitHub Actions run `{RUN_ID}` verified commit `{COMMIT}` without provider secrets,
Bluetooth hardware or a cellular call.

Automated gates that passed:

- deterministic source application and `git diff --check`;
- `py_compile` for every changed Python module/test;
- focused Phase 4 regression including prepared-stream reuse, history matching,
  partial cancellation, exact-match promotion, pre-final-not-ready fallback,
  invalidation and conversation routing;
- repository Bluetooth regression via `scripts/dev/04_test_bluetooth.sh`;
- full root pytest regression with the four already-documented baseline failures
  required to match by exact test identity and known failure signature;
- final diff validation before the verified source commit.

The first automated attempt failed before source application because a hand-built
unified diff was malformed. That bootstrap mechanism was replaced with exact
source-marker application. The first focused code run then exposed four issues:
a `ServiceLogger` call-shape error and three test synchronization/clock problems.
Those were fixed and the complete automated gate was rerun to success; no
validation was disabled or weakened.

Not measured: real Groq/Deepgram/ElevenLabs latency, PipeWire/Bluetooth behavior,
cellular transport or caller-heard audio. Those remain external final validation.
''',
)

append_once(
    "docs/DECISIONS.md",
    "BT-D26 — Accepted: implement Phase 4C as default-off exact-match prepared-stream reuse",
    f'''
## Phase 4C implementation authorization — 2026-09-15

| ID / status | Context/evidence | Decision | Consequences | Revisit only when |
|---|---|---|---|---|
| BT-D26 — Accepted: implement Phase 4C as default-off exact-match prepared-stream reuse | Earlier BT-D23 evidence remains valid: the then-current trigger had insufficient live ready-before-final frequency. The task owner later explicitly authorized implementation while postponing new real-call/provider validation. Automated run `{RUN_ID}` passed focused, Bluetooth and full baseline-aware repository gates. | Permit Phase 4C only as an explicit Bluetooth opt-in. Reuse only a first-token-ready provider stream whose final transcript, prompt and committed history still match; otherwise use the ordinary final-EOT path. Keep TTS non-speculative and leave carrier/browser/default startup unchanged. | Code correctness can be exercised without spending provider quota or requiring a phone. No caller-latency benefit is claimed from offline automation. Rollback is omission of the Phase 4C flag. | Controlled real-call evidence demonstrates benefit/regression, or a correctness/cost/provider issue requires changing the promotion contract. |
''',
)

append_once(
    "docs/CURRENT_ARCHITECTURE.md",
    "## Phase 4C optional prepared-stream seam — 2026-09-15",
    '''
## Phase 4C optional prepared-stream seam — 2026-09-15

The Bluetooth production path now has a default-off Phase 4C seam. Shadow work
may pause one provider stream after its first content token. On final EOT, only an
exact transcript/history/prompt match can transfer that same stream to the normal
`Agent`; the Agent continues token-level streaming into the existing TTS/Player
pipeline. Invalid, stale, resumed, timed-out or unavailable speculation is
cancelled and the ordinary final-EOT LLM request remains the fallback.

This does not change `process_event`, the core µ-law contract, normal server
startup, carrier/browser behavior or TTS finality. Repository automation is green;
real-call latency/audio acceptance is still pending.
''',
)

append_once(
    "docs/ROADMAP.md",
    "### Phase 4C repository status — 2026-09-15",
    '''
### Phase 4C repository status — 2026-09-15

Phase 4C prepared-response reuse is implemented behind an explicit default-off
Bluetooth flag and has passed repository-level automated verification. This is an
implementation milestone, not Phase 4 acceptance: real provider/device/cellular
validation is still required before enabling the feature by default or claiming a
latency/caller-heard improvement. Phase 5 remains unchanged.
''',
)

append_once(
    "docs/KNOWN_ISSUES.md",
    "## Phase 4C external acceptance pending — 2026-09-15",
    '''
## Phase 4C external acceptance pending — 2026-09-15

Phase 4C's default-off prepared-stream reuse has passed repository-level automated
verification, but its live benefit is intentionally unproven. The current task
defers provider/device/real-call exercise until the codebase is clean, and recent
owner evidence also reached ElevenLabs account quota exhaustion. Treat real-call
latency, audio continuity and provider behavior as pending external acceptance,
not as an automated-test failure.
''',
)

append_once(
    "context.md",
    "## 2026-09-15 — Phase 4C repository-verified implementation",
    f'''
## 2026-09-15 — Phase 4C repository-verified implementation

Task-owner authorization advanced Phase 4C implementation while explicitly
deferring real-call/provider/device testing until repository automation passed.
A default-off prepared-response seam now retains at most a first-token-ready LLM
stream, promotes it once only after exact final transcript/history/prompt match,
and otherwise falls back to ordinary final-EOT generation. TTS remains
non-speculative and carrier/browser/default startup is unchanged.

GitHub Actions run `{RUN_ID}` passed compile, focused Phase 4, complete Bluetooth,
full baseline-aware regression and diff validation; verified source commit:
`{COMMIT}`. This supersedes the earlier *implementation authorization* stop but
not the historical BT-D23 evidence or Phase 4 live-acceptance gate. No latency or
caller-heard improvement is claimed until final real-call validation is possible.
''',
)

print("Phase 4C documentation records staged")
