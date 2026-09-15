from pathlib import Path

BRANCH_REVISION = "b56a38b132951b322ed5d63059a42f0a7600f829"
WORKFLOW_RUN = "34968119711"
DATE = "2026-09-15"


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8-sig")


def write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def append_once(path: str, marker: str, block: str) -> None:
    text = read(path)
    if marker in text:
        raise SystemExit(f"{path}: documentation marker already present: {marker}")
    if not text.endswith("\n"):
        text += "\n"
    write(path, text + "\n" + block.rstrip() + "\n")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one status marker, found {count}")
    write(path, text.replace(old, new, 1))


# ROADMAP status table must move with phase evidence without claiming acceptance.
replace_once(
    "docs/ROADMAP.md",
    "| 4 | [SHUO conversation pipeline integration](phases/PHASE_04_SHUO_PIPELINE_INTEGRATION.md) | **In progress:** base integration exists; Phase 4A opt-in eager-turn measurement is being implemented; acceptance pending |",
    "| 4 | [SHUO conversation pipeline integration](phases/PHASE_04_SHUO_PIPELINE_INTEGRATION.md) | **In progress:** 4A/4B reference evidence plus default-off 4C/4D repository implementation are present; controlled provider/device/cellular validation and Phase 4 acceptance remain pending |",
)
append_once(
    "docs/ROADMAP.md",
    "### Phase 4D repository status — 2026-09-15",
    f'''### Phase 4D repository status — {DATE}

Phase 4D hardening controls are implemented at revision `{BRANCH_REVISION}` and
passed the repository-level automated gates recorded in [TESTING](TESTING.md).
The new controls remain explicit Bluetooth opt-ins: bounded incremental TTS phrase
batching, provider-visible conversation-history budgeting, content-free Groq
usage/timing capture, parallel Flux/TTS startup, and a rules-C5 2/3-frame player
pre-roll A/B control. Existing carrier/browser/default startup behavior and the
validated 15-second TTS warm-idle policy are unchanged.

This does **not** complete Phase 4. No provider/device/cellular call was executed
for this milestone, no 2-frame pre-roll or context budget was promoted to a
default, and no caller-heard latency improvement is claimed. The remaining Phase
4 gate is controlled real-path validation of the enabled 4C/4D candidates and
rollback behavior. Phase 5 and later phases remain unchanged and separately
authorized.'''
)

append_once(
    "docs/phases/PHASE_04_REALTIME_LATENCY_IMPLEMENTATION.md",
    "## Phase 4D repository implementation — 2026-09-15",
    f'''## Phase 4D repository implementation — {DATE}

Revision `{BRANCH_REVISION}` implements the repository-safe Phase 4D controls
without changing production defaults:

- `BoundedPhraseBuffer` releases punctuation-aware or hard-size-bounded fragments
  incrementally. It never waits for a complete LLM response, preserves exact text
  ordering/content, creates no timer/background task, and is enabled only with an
  explicit Bluetooth `tts_phrase_chars` value.
- LLM request history can be bounded for the provider-visible prompt only. The
  canonical in-memory history remains intact, the current user message remains,
  and the full system/digital-twin prompt sits outside the trimming budget.
  No history budget is enabled by default.
- Optional streaming usage capture requests `stream_options={{"include_usage":
  True}}` and emits only content-free token/timing metadata when the provider
  supplies it (`queue_time`, `prompt_time`, `completion_time`, `total_time`, and
  token counts). This is measurement instrumentation, not a server/network cause
  claim.
- Bluetooth startup can explicitly overlap independent Flux startup and Agent/TTS
  readiness. Failure cancels/awaits the sibling task and falls through the
  existing deterministic teardown. Serial startup remains the default.
- Player pre-roll now supports only the two values already permitted by
  `rules.md` C5: two or three 20ms frames. Three frames remains the default; the
  two-frame setting is only an A/B control pending real audio/XRUN evidence.
- The previously validated TTS pool policy (`max_idle_age=15.0`, liveness checks,
  proactive refill, Bluetooth readiness barrier) is unchanged.

### Automated implementation evidence

GitHub Actions run `{WORKFLOW_RUN}` on Ubuntu 24.04.5 / Python 3.12.14 produced:

- focused Phase 4D/related regression: **195 passed, 3 warnings**;
- complete Bluetooth regression: **149 passed, 3 warnings**;
- full root regression: **976 passed, 4 failed, 4 warnings**;
- the four failures match the documented baseline by exact identity/signature;
- `py_compile`, `git diff --check`, and staged diff validation passed.

The first full-suite attempt found one new constructor-bypass compatibility
failure in `test_the_token_loop_actually_accumulates`; the implementation was
corrected so absent Phase-4D Agent fields take the legacy/default-off path, then
all applicable gates were rerun to the result above. No validation was skipped or
weakened.

### Phase 4D stop boundary

Repository-level implementation is complete for the current authorized hardening
slice, but Phase 4 acceptance is **not** complete. No live provider/device/cellular
exercise was performed in this milestone. Phrase sizing, history budget,
parallel-startup benefit, provider/server timing interpretation, two-frame pre-roll
quality/XRUN behavior, prepared-stream reuse benefit, and caller-heard latency
still require a controlled real-path comparison before any candidate becomes a
default or any latency target is claimed. Phase 5 is not advanced.'''
)

append_once(
    "docs/phases/PHASE_04_SHUO_PIPELINE_INTEGRATION.md",
    "## Phase 4D repository milestone — 2026-09-15",
    f'''## Phase 4D repository milestone — {DATE}

The default-off Phase 4D hardening slice is implemented at `{BRANCH_REVISION}`.
It adds bounded incremental TTS phrase grouping, optional provider-visible history
budgeting that never trims the system/digital-twin prompt or canonical history,
optional content-free Groq usage timing, optional fail-clean parallel service
startup, and a controlled 2/3-frame pre-roll A/B setting. The validated 15-second
TTS warm pool remains unchanged and three pre-roll frames remain the default.

Automated repository validation passed: 195 focused tests, 149 Bluetooth tests,
and a full root run with 976 passes plus only the four documented baseline
failures. No provider/device/cellular call was used. Therefore Phase 4 remains
**IN PROGRESS**: the remaining acceptance gate is controlled real-path validation
of correctness, latency, audio quality/XRUN behavior and rollback. Phase 5 and
later phases are not authorized by this milestone.'''
)

append_once(
    "docs/CURRENT_ARCHITECTURE.md",
    "### Phase 4D default-off hardening controls — 2026-09-15",
    f'''### Phase 4D default-off hardening controls — {DATE}

Revision `{BRANCH_REVISION}` adds optional Bluetooth-only hardening controls while
preserving the existing default pipeline. `Agent` can use a bounded incremental
phrase buffer before `TTSService.send`; without the explicit option it continues
the previous token-by-token path. `LLMService` can bound only the provider-visible
conversation-history suffix and can request streaming usage metrics; canonical
history and the complete system/digital-twin prompt remain retained locally and
outside that budget. The same prompt budget is applied to prepared shadow streams
when explicitly enabled so final matching remains coherent.

`run_bluetooth_conversation` can explicitly overlap Flux startup with async
Agent/TTS readiness and records content-free startup spans; serial startup remains
default. `AudioPlayer` accepts only two or three pre-roll frames per rules C5 and
keeps three as default. The existing 15-second TTS warm-idle/readiness behavior is
unchanged. None of these controls are imported or started by default `main.py`.
Offline automation proves wiring, bounds, cleanup and regressions, not live
provider timing, caller-heard latency, XRUN/audio quality or production benefit.'''
)

append_once(
    "docs/TESTING.md",
    "## Phase 4D repository hardening regression — 2026-09-15",
    f'''## Phase 4D repository hardening regression — {DATE}

Implementation revision: `{BRANCH_REVISION}`. Final automated GitHub Actions run:
`{WORKFLOW_RUN}` on Ubuntu 24.04.5 with CPython 3.12.14. No provider credentials,
phone, PipeWire call stream or cellular call were used by this gate.

Final results:

- changed Python compilation: **PASS**;
- focused Phase 4D + Phase 4C/speculation/player/Flux/TTS + exact legacy Agent
  monitor regression: **195 passed, 3 warnings**;
- `scripts/dev/04_test_bluetooth.sh`: **149 passed, 3 warnings**;
- full root suite: **976 passed, 4 failed, 4 warnings**;
- `git diff --check` and staged diff check: **PASS**.

The four full-root failures are exactly the documented baseline identities:

1. `scripts/test_v2_keys.py::test_shunya_key` — async test collection signature;
2. `scripts/test_v2_keys.py::test_azure_key` — async test collection signature;
3. `tests/test_config_api.py::TestIsolation::test_the_call_server_has_no_config_routes`
   — `_IncludedRouter` path signature;
4. `tests/test_test_call.py::TestTheProcessSplitSurvives::test_the_call_server_has_no_test_call_routes`
   — `_IncludedRouter` path signature.

The fix/test cycle was exercised rather than bypassed: an earlier full run had a
fifth failure,
`tests/test_call_monitor.py::TestItSeesARealCall::test_the_token_loop_actually_accumulates`,
because that legacy isolation test constructs `Agent` with `__new__`. The Agent
callback was made backward-compatible with absent Phase-4D fields; that exact test
was added to focused validation; focused, Bluetooth and full suites were rerun and
the new failure disappeared.

Limit: this evidence establishes repository correctness only. It does not select
phrase/history/pre-roll defaults and does not establish provider/server latency,
Bluetooth XRUN/audio quality, prepared-stream latency gain, or caller mouth-to-ear
performance. Those remain controlled real-path Phase 4 acceptance evidence.'''
)

append_once(
    "docs/KNOWN_ISSUES.md",
    "## Phase 4D live-evidence limits — 2026-09-15",
    f'''## Phase 4D live-evidence limits — {DATE}

The Phase 4D controls at `{BRANCH_REVISION}` are repository-verified but are not
live-qualified. In particular:

- `tts_phrase_chars` has no selected production value; batching too aggressively
  can increase first-speech delay and batching too little may not improve TTS
  synthesis behavior;
- `llm_history_max_chars` has no default budget; any production value must preserve
  digital-twin conversational fidelity even though system facts/rules and
  canonical history are structurally retained;
- Groq usage/timing fields are optional provider telemetry and do not by themselves
  identify client/network versus provider causes unless compared with client spans;
- `parallel_startup` is default-off pending live startup comparison;
- two-frame playback pre-roll is default-off pending Bluetooth audio quality/XRUN
  evidence; the current three-frame default remains;
- Phase 4C prepared-stream reuse and these Phase 4D controls have not yet been
  jointly validated on a controlled real call.

No sub-500ms or other caller-heard latency claim follows from the offline pass.'''
)

append_once(
    "docs/DECISIONS.md",
    "## BT-D27 — Phase 4D hardening remains default-off pending live evidence — 2026-09-15",
    f'''## BT-D27 — Phase 4D hardening remains default-off pending live evidence — {DATE}

Revision `{BRANCH_REVISION}` passed focused, complete Bluetooth and baseline-aware
full-root automation. Keep Phase 4D as explicit Bluetooth controls rather than new
production defaults: bounded incremental TTS phrase grouping, provider-visible
history budgeting that preserves full system/digital-twin facts and canonical
history, content-free provider timing, fail-clean parallel startup, and rules-C5
2/3-frame pre-roll selection. Retain the previously live-validated 15-second TTS
warm-idle/readiness policy and the three-frame pre-roll default. The first full
regression exposed and then verified the fix for an Agent constructor-bypass
compatibility issue.

Decision consequence: repository implementation is ready for controlled real-path
comparison, but there is not enough evidence to choose phrase/history/pre-roll
values, enable parallel startup or prepared reuse by default, or claim lower
caller-heard latency. Rollback is omission of the explicit controls. Phase 4
acceptance and Phase 5 advancement remain separate gates.'''
)

append_once(
    "docs/COMPATIBILITY.md",
    "## Phase 4D compatibility note — 2026-09-15",
    f'''## Phase 4D compatibility note — {DATE}

The Phase 4D implementation at `{BRANCH_REVISION}` does not expand the supported
hardware matrix. All new tuning is attached to the explicit Bluetooth runner and
is default-off; carrier/browser/default production startup remains unchanged.
Player pre-roll is constrained to the already permitted two/three-frame range,
with three retained as default. No claim is made that another phone, PipeWire
version, codec/profile, provider region or operating system supports the reference
Bluetooth path until independently qualified.'''
)

append_once(
    "context.md",
    "## 2026-09-15 — Phase 4D repository hardening milestone",
    f'''## {DATE} — Phase 4D repository hardening milestone

Task-owner authorization to continue Phase 4 through 4D was applied without
advancing later phases. Revision `{BRANCH_REVISION}` adds default-off bounded TTS
phrase grouping, provider-visible history budgeting with full system/digital-twin
prompt and canonical history preservation, optional content-free Groq usage timing,
fail-clean parallel Bluetooth startup, and a rules-C5 two/three-frame player
pre-roll A/B control. The earlier validated 15-second TTS warm-pool policy was not
changed.

Automated evidence: GitHub Actions `{WORKFLOW_RUN}` passed 195 focused tests and
149 complete Bluetooth tests; the full root result was 976 passed / 4 failed / 4
warnings, with only the exact documented baseline failure identities/signatures.
An initial full run exposed a new `Agent.__new__()` compatibility regression; it
was fixed and the entire relevant gate was rerun rather than bypassed. No live
provider/device/cellular call was performed. Phase 4 remains open only for
controlled real-path acceptance evidence; no new default or latency claim and no
Phase 5 advancement follows from this repository milestone.'''
)

print("Phase 4D documentation records applied")
