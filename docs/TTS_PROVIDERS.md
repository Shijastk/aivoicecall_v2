# TTS provider routing and eSpeak fallback

**Status:** implemented in repository; local/reference-hardware regression and live validation remain required before acceptance claims.

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

The local provider does not buffer a complete model answer. It uses the existing deterministic `BoundedPhraseBuffer` with a 24-character cap, synthesizes one bounded phrase at a time through a shell-free subprocess, converts it in memory and immediately forwards the resulting mu-law chunk to the existing player.

A live/reference-machine benchmark is still required before making any eSpeak latency claim. A local provider being fast in isolation is not evidence of caller mouth-to-ear latency.

## Fallback semantics

The emergency wrapper validates eSpeak availability when the pooled service starts. While ElevenLabs has produced no audio, it retains only a bounded pre-audio replay window (maximum 4096 characters). If ElevenLabs fails before its first audio chunk, that unheard text is replayed through eSpeak and the turn continues.

Once any ElevenLabs audio has been emitted, same-turn replay is permanently disabled. If ElevenLabs later fails mid-answer, SHUO does **not** restart the response from the beginning in eSpeak because that would duplicate speech the caller already heard. The current turn completes/truncates through the existing failure path; a later turn may use a newly prepared provider service.

If the bounded replay window is exceeded before first audio, replay fallback is disabled for that turn instead of retaining unbounded text.

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

## Focused regression commands

After pulling the implementation, run before any live call:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
python -m pytest -q \
  tests/test_tts_provider.py \
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

Do not mark the implementation accepted merely because the new unit tests exist. Local test execution and the separately authorized live evidence remain required.
