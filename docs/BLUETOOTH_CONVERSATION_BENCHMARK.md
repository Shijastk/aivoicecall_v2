# Bluetooth conversation benchmark — evidence contract

**Status: V2 BASE OWNER-EXECUTED ON 2026-09-15; REAL-FLUX EXTENSION IN THIS PATCH NOT YET EXECUTED.**

This development harness is additive and opt-in. It does not change `main.py`,
carrier/browser behavior, Bluetooth production defaults, Phase 4 speculation, or
call control. It is designed to automate repeatable regression evidence without
turning synthetic data into a live-latency claim.

## Authority and preserved boundaries

The harness follows `AGENTS.md`, `CLAUDE.md`, `rules.md`, `docs/README.md`,
`CURRENT_ARCHITECTURE.md`, `REQUIREMENTS.md`, `TESTING.md`, `KNOWN_ISSUES.md`,
`DECISIONS.md`, `ROADMAP.md`, the Phase 4 contract, and the current source at
commit `46afb9c736a40def31844daa0b14a223f1ea9aec`.

Load-bearing constraints:

- `process_event` stays pure; the patch does not edit `shuo/state.py`.
- no carrier/browser/default-startup changes;
- no new dependency;
- no secret or `.env` reader;
- no live provider access unless the operator passes an explicit network flag;
- no live Deepgram, PipeWire, Bluetooth, cellular call or call-control action from
  offline/provider modes;
- no caller-heard claim from local Bluetooth adapter writes;
- missing measurements are `NOT_MEASURED`, never numeric zero;
- comparison refuses metrics whose scope/start/end/clock provenance differs.

## Why the modes are separate

### `offline`

Uses the real Bluetooth conversation orchestrator, real Flux message parser, real
`Agent`, real `AudioPlayer`, and real Bluetooth outbound adapter, while replacing
LLM/TTS and hardware with injected fakes. It exercises event ordering, barge-in,
Agent cancellation, player clear, duplicate-final suppression and new-answer
restart.

Its timing values are **local synthetic regression measurements**, not provider,
STT, Bluetooth or mouth-to-ear latency.

### `providers`

Requires `--allow-provider-network`. It uses real Groq and ElevenLabs through the
current `Agent`/`TTSPool`, but injects Flux turn events and uses an in-memory
Bluetooth session. It can therefore measure LLM/TTS/network behavior without a
phone call, while explicitly excluding Deepgram turn detection, PipeWire,
Bluetooth radio, cellular network and handset playback.

It uses a synthetic benchmark prompt and synthetic facts. Quality checks are
boolean instruction/grounding checks; there is no invented 0–10 "quality score".

### `log`

Parses an existing content-free live Bluetooth log. It extracts only fields whose
boundaries are present in the log. `Playback dispatched` remains local dispatch,
not caller-heard audio. Log-clock correlations use the printed millisecond clock
and are labelled separately from source-emitted monotonic durations.

### `compare`

Compares medians only when metric name, scope, start boundary, end boundary and
clock are identical. It emits an observed delta only. It does not invent a
significance threshold and does not claim causality.

## Clock contract

New benchmark instrumentation uses `time.perf_counter_ns()` and converts integer
nanosecond differences to milliseconds only after subtraction. This follows the
repository's `rules.md` requirement to use the high-resolution performance clock
for timing harnesses and avoids float precision loss in the benchmark clock.

Existing production log values retain their own source clocks and resolution;
the analyzer does not silently relabel them as `perf_counter_ns` measurements.

## Commands after applying the patch

Provider/device-free regression:

```bash
PYTHON_DOTENV_DISABLED=1 PYTHONPATH=. \
  .venv/bin/python scripts/dev/15_conversation_benchmark.py offline \
  --json-out /tmp/shuo-bench-offline.json
```

Real Groq + ElevenLabs, no STT/device/call:

```bash
PYTHON_DOTENV_DISABLED=1 LLM_MODEL=qwen/qwen3.8-27b PYTHONPATH=. \
  .venv/bin/python scripts/dev/15_conversation_benchmark.py providers \
  --allow-provider-network \
  --json-out /tmp/shuo-bench-providers.json
```

Analyze a separately authorized live run:

```bash
PYTHONPATH=. .venv/bin/python scripts/dev/15_conversation_benchmark.py log \
  /tmp/bt-bargein-proof.log \
  --json-out /tmp/shuo-bench-live.json
```

Compare like-for-like reports:

```bash
PYTHONPATH=. .venv/bin/python scripts/dev/15_conversation_benchmark.py compare \
  /tmp/before.json /tmp/after.json \
  --json-out /tmp/comparison.json
```

Focused tests:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
  .venv/bin/python -m pytest -q tests/test_conversation_benchmark.py \
  -p no:cacheprovider
```

Then run the repository's existing Bluetooth and full regression scripts and
compare the four documented baseline failures by identity/signature. Do not record
pass counts in `TESTING.md` until they have actually been executed in the owner's
environment.

## Public documentation checked before design

- Deepgram Flux state documentation: `StartOfTurn` is the recommended barge-in
  signal; `TurnResumed` follows an eager candidate; `Update` messages are emitted
  approximately every 0.25 seconds of transcribed audio; `EndOfTurn` owns final
  turn completion semantics.
- Deepgram Flux reference: current public valid eager range extends beyond the
  repository's local 0.3–0.7 safety restriction. The harness does **not** widen
  the repository restriction.
- Python 3.12 `time` documentation: `perf_counter_ns()` is intended for
  high-resolution duration measurement without float precision loss.
- ElevenLabs public WebSocket documentation confirms inactivity timeout behavior,
  but this patch does not change the repository's separately validated 15-second
  TTS pool policy.

Public vendor documentation is contextual evidence only. Repository decisions and
measured local evidence continue to govern current behavior unless an explicit
change is separately authorized and tested.


## Barge-in clear interpretation

A normal interruption does not imply a playback clear. `Agent.cancel_turn` clears
Bluetooth playback only when the per-turn `AudioPlayer` is already playing. If the
caller interrupts during LLM/TTS startup before the player starts, cancellation can
return correctly without any `PlaybackClear` event. Live-log analysis therefore
checks each cancellation block for `cancel_stage=player_begin`; only those blocks
require a matching `PlaybackClear_returned`.


## Owner-executed V2 evidence — 2026-09-15

The task owner executed the V2 harness on commit `46afb9c...` with the patch
uncommitted/dirty. Recorded results:

- focused benchmark: **7 passed, 3 warnings**;
- Bluetooth regression: **127 passed, 3 warnings**;
- full root suite: **948 passed, 4 failed, 4 warnings**;
- the four failures match the documented baseline identities/signatures;
- corrected live-log analysis observed **7** normal barge-ins, **4** cancellations
  with an active player, **4** required playback clears, **0** missing required
  clears, and **3** cancellations before playback started.

The V2 provider-only mode also completed with real Groq+ElevenLabs while Flux
turn events were injected. That is provider evidence but not Deepgram or device
E2E evidence. Do not promote those timings to caller-heard latency.

## Real Flux provider-pipeline extension

This patch adds two explicit commands and does not edit the production Flux,
Agent, state machine, Bluetooth session, carrier, browser, or default startup.

### 1. Freeze one synthetic caller fixture

`prepare-flux-fixture` uses the repository's current ElevenLabs `TTSService` to
synthesise the fixed sentence:

```text
What is the benchmark codeword?
```

The returned `ulaw_8000` bytes are converted once through the repository-owned
`BluetoothOutboundCodec` into the validated Bluetooth boundary format
S16LE/16kHz/mono. The exact PCM bytes are written to disk with a manifest that
records SHA-256, byte count, sample contract, source text, generator and voice ID.

Fixture generation is **not** part of any latency measurement. Benchmark runs
refuse a fixture whose byte count, SHA-256 or audio contract does not match its
manifest. Before/after comparisons also embed fixture hash, LLM model, TTS model
and agent voice ID in metric provenance, so a different stimulus/model/voice is
not silently compared as like-for-like.

Command:

```bash
PYTHON_DOTENV_DISABLED=1 PYTHONPATH=. \
  .venv/bin/python scripts/dev/15_conversation_benchmark.py \
  prepare-flux-fixture --allow-provider-network
```

Default artifacts:

```text
var/benchmark/flux_codeword.s16le
var/benchmark/flux_codeword.json
```

Re-running does not overwrite them unless `--overwrite` is explicit.

### 2. Replay the frozen bytes through the real provider pipeline

`flux-pipeline` creates a fresh call-like provider session for every sample:

```text
frozen S16LE/16k synthetic source
  -> real Bluetooth inbound codec
  -> real Deepgram Flux (flux-general-en, mulaw/8k)
  -> real SHUO state/action orchestration
  -> real Groq LLM
  -> pre-warmed real ElevenLabs TTS
  -> real AudioPlayer + Bluetooth outbound codec
  -> in-memory local sink
```

There is no PipeWire, Bluetooth device/radio, phone, carrier, cellular network or
handset in this mode. It therefore reports `PROVEN_PROVIDER_PIPELINE_NO_DEVICE`,
never caller-heard or cellular mouth-to-ear latency.

Every sample uses the same fixture bytes, a fresh Deepgram connection and fresh
Agent history. TTS readiness is awaited before speech begins, matching the current
Bluetooth startup barrier. Connection setup occurs before the measured speech
boundary and is not folded into response latency.

The synthetic source emits one S16LE frame every 20ms against
`time.perf_counter_ns()` deadlines and continues digital silence after speech,
so Flux performs natural end-of-turn detection. The harness does **not** send
`ForceEndTurn`, alter `eot_threshold`, or enable eager EOT. Source scheduling
lateness is recorded rather than hidden.

Measured boundaries include:

- source first frozen caller-audio frame -> Deepgram `StartOfTurn` callback;
- source final frozen caller-audio frame -> Deepgram `EndOfTurn` callback;
- final converted caller-fixture `FluxService.send` return -> `EndOfTurn` callback;
- `EndOfTurn` callback -> `Agent.start_turn`;
- Agent start -> first Groq token;
- first token -> first ElevenLabs audio;
- final caller-fixture frame -> first TTS audio callback;
- Deepgram EOT -> first local outbound write.

The TTS-generated caller fixture may contain leading/trailing non-speech. Therefore fixture-final-frame -> EOT is a repeatable provider-pipeline boundary, **not** an acoustic speech-end measurement.

The report also stores Deepgram `EndOfTurn.trigger` values and deterministic word
error rate against the synthetic source sentence. A boolean end-to-end grounding
check requires the final generated answer to contain the synthetic codeword
`ORBIT-7`; this is not a general naturalness score.

Run five samples:

```bash
PYTHON_DOTENV_DISABLED=1 \
LLM_MODEL=qwen/qwen3.8-27b \
PYTHONPATH=. \
.venv/bin/python scripts/dev/15_conversation_benchmark.py \
  flux-pipeline --allow-provider-network --runs 5 \
  --json-out /tmp/shuo-bench-flux-pipeline.json
```

For the first execution, treat all returned values as new evidence. Do not write
them into `TESTING.md` until focused tests, Bluetooth regression, full regression
and `git diff --check` have also been run and the known four failure identities
remain unchanged.

### Percentile interpretation

The report continues to print the empirical nearest-rank p95 for consistency with
existing reports, but records an explicit caution in metadata: with a small sample
such as five runs, p95 is effectively near/the maximum observed sample and is not
a population-tail guarantee. Use raw samples and median for early debugging; use
more runs before interpreting tail stability.

## Public documentation re-checked for the real-Flux extension

Deepgram public documentation checked 2026-09-15 states:

- Flux raw audio supports `mulaw` with explicit sample rate; the repository's
  existing Flux connection uses mulaw/8000 and is left unchanged;
- `StartOfTurn` is the recommended barge-in signal;
- `EndOfTurn` includes a `trigger` (`model`, `manual`, `timeout`, open to future
  values); the harness records it but does not hard-code an allowlist;
- current default `eot_timeout_ms` is 5000ms;
- `ForceEndTurn` exists, but this benchmark deliberately does not use it because
  the goal is to measure the repository's natural Flux turn-finalization path;
- current public eager ranges are broader than the repository's local safety
  restriction; this patch changes neither.

The real-Flux extension is still Phase-4 provider-pipeline evidence only. The
repository's Phase-5 contract separately owns controlled cellular E2E validation,
and Phase 6 owns automated call-control lifecycle. This harness does not advance
either phase.
## Relationship to the Android cellular TX harness — 2026-09-23

The provider-only `human-sim` benchmark and the Android ADB cellular TX
harness are different evidence surfaces. The former has no device/cellular
transport. The latter has reference-proven real cellular **transmit** from an
itel caller phone, but its reverse/downlink receive path is not yet validated.

Neither surface alone is a fully automated real-cellular conversation benchmark,
and neither may be used to manufacture a caller-heard latency measurement.
## Android caller-side receive evidence — 2026-09-23

The real-cellular caller-side receive capability is no longer hypothetical:
upstream scrcpy 4.1 captured the itel P683L's `VOICE_DOWNLINK` during a real
manual cellular call and delivered clear Galaxy A10 speech to Ubuntu. With
headphone monitoring, the owner reported no echo.

The SHUO-owned receive bridge remains a separate candidate until its own bounded
reference probe passes. The provider-only `human-sim` still has no cellular
transport, and no surface may be used to infer caller-heard latency.
## Android caller-side RX transport qualified — 2026-09-23

The SHUO-owned receive bridge has now independently passed on the reference itel
P683L, so the caller-side real-cellular harness has validated TX and RX
transport directions. This remains distinct from the provider-only human-sim
benchmark and from a complete closed-loop synthetic-human conversation
controller. No caller-heard latency is inferred from the transport probe.
## Real-cellular closed-loop caller candidate — 2026-09-23

A new controller now differs from the provider-only `human-sim` surface in one
important way: it traverses the real itel <-> Galaxy cellular connection and
uses the reference-qualified ADB receive/transmit boundaries.

It still does not automate call control or create an external caller-heard clock.
Its report is content-free: response text is used only in memory to score
deterministic continuity booleans. The branch remains live-gated before merge.

## First-turn warmup A/B — 2026-09-26

For the controlled reference-path A/B, keep the established runner arguments and
add `--llm-warmup --parallel-startup` only to the candidate run. The warmup is
default-off, uses static non-conversation input, and does not alter the approved
Flux EOT threshold or the manual call-control boundary.

Report the content-free `LLMWarmup`, first real `LLMRequest` stream-open /
first-token timing, first `TTS first audio`, and first
`PlaybackFirstWrite_returned` timing. Later turns are the warm-path comparison.
Do not equate these local timestamps with caller-heard mouth-to-ear latency.
