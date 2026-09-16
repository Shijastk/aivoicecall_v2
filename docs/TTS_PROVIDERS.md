# TTS provider routing and eSpeak fallback

**Status:** implementation is automated-regression validated; local/reference-hardware live validation remains required before Phase 5 or latency acceptance claims.

## Purpose

ElevenLabs remains SHUO's production-quality primary TTS. The local eSpeak path exists for two narrow reasons:

1. cost-free development / heavy functional testing; and
2. an opt-in emergency fallback so a pre-audio ElevenLabs failure does not leave a turn silent.

This does not make eSpeak voice quality or latency equivalent to ElevenLabs, and eSpeak evidence must not be used as ElevenLabs-specific latency/voice-quality evidence.

## Configuration

Default behavior is unchanged:

```bash
TTS_PROVIDER=elevenlabs
TTS_FALLBACK_PROVIDER=
```

Cost-free testing can bypass ElevenLabs completely:

```bash
TTS_PROVIDER=espeak
TTS_FALLBACK_PROVIDER=
```

Production may keep ElevenLabs primary and opt into the local emergency fallback:

```bash
TTS_PROVIDER=elevenlabs
TTS_FALLBACK_PROVIDER=espeak
```

With that fallback configuration, an absent `ELEVENLABS_API_KEY` selects eSpeak without contacting ElevenLabs. With a configured key, ElevenLabs remains primary.

## System dependency

eSpeak is an optional operating-system dependency, not a Python package dependency. On Ubuntu:

```bash
sudo apt update
sudo apt install -y espeak-ng
espeak-ng --version
```

The provider prefers `espeak-ng` and accepts a compatible `espeak` binary if already installed. Python 3.13+ also needs the repository's already-documented `audioop-lts` compatibility shim when eSpeak PCM conversion is used.

If either `TTS_PROVIDER=espeak` or `TTS_FALLBACK_PROVIDER=espeak` is configured for `main.py`, startup fails clearly when no eSpeak binary is installed. Default ElevenLabs-only startup does not acquire this dependency.

## Audio contract

eSpeak emits PCM WAV internally. `shuo/services/tts_espeak.py` contains that PCM entirely inside the provider boundary and converts each bounded phrase to mono G.711 mu-law / 8 kHz before calling the existing `on_audio` callback.

Therefore the SHUO/player/carrier contract remains unchanged:

```text
provider boundary -> base64 mu-law / 8 kHz / mono -> AudioPlayer -> carrier/Bluetooth adapter
```

No raw audio file is written and no PCM/L16 carrier route is added.

## Streaming and latency safety

The direct local provider does not wait for a complete model answer. It uses the existing deterministic `BoundedPhraseBuffer` with a 24-character cap, synthesizes one bounded phrase at a time through a shell-free subprocess, converts it in memory and immediately forwards the resulting mu-law chunk to the existing player.

The ElevenLabs+eSpeak emergency wrapper also preserves the live streaming seam required by `rules.md` A4: every `send()` is forwarded to the active primary immediately. Before the first ElevenLabs audio arrives, the wrapper keeps a **shadow recovery copy** of already-forwarded text, capped at 4096 characters. That copy never gates token delivery or waits for LLM completion. If the cap would be exceeded, the shadow copy is cleared and same-turn replay is disabled rather than growing without bound. `tests/test_tts_streaming_contract.py` pins both immediate forwarding and the hard cap.

A live/reference-machine benchmark is still required before making any eSpeak latency claim. A local provider being fast in isolation is not evidence of caller mouth-to-ear latency.

## Fallback semantics

The emergency wrapper validates eSpeak availability when the pooled service starts. While ElevenLabs has produced no audio, it retains only the bounded shadow recovery copy described above. If ElevenLabs fails before its first audio chunk, that already-forwarded but unheard text is replayed through eSpeak and the turn continues.

Once any ElevenLabs audio has been emitted, same-turn replay is permanently disabled. If ElevenLabs later fails mid-answer, SHUO does **not** restart the response from the beginning in eSpeak because that would duplicate speech the caller already heard. The current turn completes/truncates through the existing failure path; a later turn may use a newly prepared provider service.

If the bounded shadow window is exceeded before first audio, replay fallback is disabled for that turn instead of retaining unbounded text.

## Provider selection invariants

- no new state-machine event/action is introduced;
- `process_event` remains pure;
- existing `TTSPool` lifecycle and test monkeypatch seam are preserved;
- default environment remains ElevenLabs-only;
- `TTS_PROVIDER=espeak` never constructs or contacts ElevenLabs;
- no secret is logged or inspected;
- eSpeak subprocesses use argument arrays + stdin/stdout, never a shell;
- cancellation terminates/kills an owned child with bounded waits;
- eSpeak is not a Phase 6 call-control feature.

## Automated validation — 2026-09-16

The implementation source candidate was validated in GitHub Actions on Ubuntu 24.04 with CPython 3.12.14 and `espeak-ng` 1.51. The CI-only branch differed from the source candidate only by `.github/workflows/espeak-validation.yml`; that temporary workflow is not part of the production source commit.

Two completed CI runs succeeded. The stronger second run (`35117165791`) established:

- real installed `espeak-ng` subprocess -> in-memory WAV -> mu-law conversion: **PASS**, 3 audio chunks / 22,241 mu-law bytes;
- direct `TTS_PROVIDER=espeak` with no ElevenLabs key: **PASS**;
- ElevenLabs primary + configured eSpeak fallback with no ElevenLabs key: **PASS** and no ElevenLabs requirement at startup;
- default ElevenLabs-only configuration with a missing key: **fail-closed PASS**;
- focused TTS/production regression: **54 passed, 3 warnings**;
- complete Bluetooth regression: **162 passed, 3 warnings**;
- full repository regression: **999 passed, 4 failed, 4 warnings**;
- the four full-suite failures matched the exact documented historical identities/signatures; no new failure identity appeared.

The full-suite baseline failures remain the two unsupported unmarked async Shunya/Azure probes and the two `_IncludedRouter.path` isolation-test failures. They were not changed or bypassed.

A later source-contract test additionally pins that the bounded pre-audio shadow copy never delays primary `send()` calls and disables itself rather than exceeding its cap. That final test must pass in the same focused/Bluetooth/full baseline-aware sequence before the final source revision is moved to `main`.

These automated results establish code-path, subprocess, codec-boundary and regression behavior only. They do **not** prove eSpeak caller-heard latency, voice quality, real-call fallback during an actual ElevenLabs outage, Phase 5 acceptance, or Phase 6 readiness.

## Focused regression commands

After pulling the implementation, run before any live call:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
python -m pytest -q \
  tests/test_tts_provider.py \
  tests/test_tts_streaming_contract.py \
  tests/test_tts_failure.py \
  tests/test_bluetooth_production.py \
  tests/test_bluetooth_conversation.py \
  tests/test_player.py \
  -p no:cacheprovider
```

Then the Bluetooth suite:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
python -m pytest -q tests/test_bluetooth_*.py -p no:cacheprovider
```

Then the full repository suite:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
python -m pytest -q -p no:cacheprovider
```

The historical full-suite baseline contains four unrelated failures. Compare failure **identities/signatures**, not only totals; any new failure identity is a regression until explained.

Do not mark Phase 5 accepted merely because the automated provider tests pass. The separately authorized live Gate 1 evidence and remaining Phase 5 measurement gates still control phase acceptance.
