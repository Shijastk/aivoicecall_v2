# TTS provider routing and local Pocket TTS

**Status:** Pocket TTS is the approved replacement candidate for the prior eSpeak local testing/fallback path. Repository automated validation must be baseline-clean before merge; real reference-hardware validation is still required before Phase 5 or caller-heard latency/quality claims.

## Purpose

ElevenLabs remains SHUO's production-quality default TTS. Pocket TTS is the local CPU provider for two narrow reasons:

1. cost-free development / heavy functional testing with speech that is materially clearer than the prior eSpeak path; and
2. an opt-in pre-audio emergency fallback so an ElevenLabs failure can continue locally without replaying speech the caller already heard.

Pocket evidence is not ElevenLabs evidence. A local Pocket benchmark is also not caller mouth-to-ear evidence.

## Why Pocket replaced eSpeak — 2026-09-16

The owner reported that eSpeak was too generic/robotic to reliably understand during live functional testing. A suspected repeat/loop was then isolated with transcript-bearing local diagnostics: the AI had generated one normal response per caller turn and there was no new STT StartOfTurn/EndOfTurn during the supposedly repeated playback. A direct eSpeak sentence also completed normally in 2.54 seconds with no fatal error. The incident therefore did not establish a conversation feedback loop; it exposed poor test-voice intelligibility.

Pocket TTS and Supertonic 3 were then benchmarked outside the repository on the reference MSI laptop before any provider code was changed. Pocket native streaming produced first audio at roughly 101–105 ms in the three-sentence comparison and a 20-run warm stability test produced **20/20 successful generations, 0 failures, 75.1–88.8 ms TTFA, 78.3 ms average TTFA**. Supertonic 3's measured 18–21 character synthesis completion was roughly 467–490 ms. Telephone-band 8 kHz G.711 mu-law A/B listening favored Pocket in two of three blind pairs. Those measurements justify Pocket as the better local functional-test candidate for this reference machine; they do not establish caller-heard latency or universal voice-quality superiority.

## Configuration

Default production behavior remains unchanged:

```bash
TTS_PROVIDER=elevenlabs
TTS_FALLBACK_PROVIDER=
```

Cost-free local functional testing:

```bash
TTS_PROVIDER=pocket
TTS_FALLBACK_PROVIDER=
```

Optional ElevenLabs primary with local pre-audio fallback:

```bash
TTS_PROVIDER=elevenlabs
TTS_FALLBACK_PROVIDER=pocket
```

With the fallback configured, an absent `ELEVENLABS_API_KEY` selects Pocket directly without constructing/contacting ElevenLabs. With a key present, ElevenLabs stays primary and Pocket is pre-warmed for the restricted pre-audio fallback path.

`TTS_PROVIDER=espeak` and `TTS_FALLBACK_PROVIDER=espeak` are no longer supported runtime selections after this replacement.

## Optional dependency profile

Pocket is deliberately not added to the default `requirements.txt`, because ElevenLabs remains the default provider and Pocket pulls a CPU PyTorch/model stack that default production does not need. Install the explicit local profile instead:

```bash
python -m pip install -r requirements-pocket-tts.txt
```

`requirements-pocket-tts.txt` pins `pocket-tts==3.1.0`, includes the base requirements and adds the PyTorch CPU index. The CPU index is important on Linux because the normal PyPI Torch path can otherwise pull several gigabytes of CUDA runtime packages that Pocket does not require.

### Python 3.13+ audio compatibility

CPython removed the stdlib `audioop` module in Python 3.13. SHUO's Pocket provider keeps native PCM inside the provider boundary and uses the stdlib-compatible `audioop` API for stateful resampling and G.711 mu-law encoding; the existing Bluetooth codec also uses that API. The Pocket dependency profile therefore conditionally installs `audioop-lts==0.2.2` only on Python 3.13+ while Python 3.12 and older continue using the stdlib module.

This requirement was added from direct reference-host evidence on 2026-09-17: a fresh Python 3.14 Pocket install loaded the model but emitted zero SHUO audio chunks and reported `Pocket TTS PCM conversion requires audioop`. No real call was attempted after that failure. The corrected profile was then exercised by GitHub Actions run `35182574743` on both Python 3.12 and Python 3.14: dependency install, `audioop` import, real Pocket synthesis, focused Pocket/production tests, the Bluetooth suite and the full repository baseline verifier all completed successfully in both jobs. This compatibility result does not substitute for the separate reference-hardware cellular call gate.

The first model/voice use may populate the Hugging Face cache. SHUO uses Pocket's built-in catalog voice alias `alba` by default. This is intentional: the first automated real-package validation attempt used an `hf://...wav` prompt, which Pocket 3.1.0 correctly interpreted as voice cloning and rejected without gated cloning weights. The implementation was corrected from that evidence to the ungated built-in catalog alias. No Hugging Face token or cloned voice is required for the supported SHUO local path.

An optional `POCKET_TTS_VOICE` can select another Pocket catalog voice. Custom/clone voice URLs are outside the approved reference path and must not be treated as already validated.

## Audio contract

Pocket generates native float PCM (normally 24 kHz) only inside `shuo/services/tts_pocket.py`. Each native streaming chunk is converted in-memory to mono PCM16, statefully resampled to 8 kHz within the phrase, encoded as G.711 mu-law and base64-wrapped before the existing callback is invoked.

The core contract therefore remains:

```text
provider boundary -> base64 G.711 mu-law / 8 kHz / mono -> AudioPlayer -> carrier/Bluetooth adapter
```

No raw audio file is written. No L16/PCM route is added to the carrier/core path.

## Streaming and boundedness

The local provider does not wait for a complete LLM response. It keeps the established deterministic `BoundedPhraseBuffer` with a 24-character hard cap. Once a bounded phrase is available, Pocket's synchronous CPU generator runs off the asyncio event loop and `generate_audio_stream()` native chunks cross back through a bounded four-chunk queue. Each chunk is converted and forwarded immediately.

This preserves the A4 rule: there is no whole-answer TTS gate. The 24-character phrase seam is intentionally retained from the already-tested local-provider design rather than inventing a new unmeasured batching threshold.

Pocket model/voice state is process-local and reused so TTSPool service refill does not reload the model on every turn. Native inference is serialized because one shared model instance must not be driven concurrently by overlapping provider objects.

## Cancellation

A Pocket generation owns a cooperative cancellation event and a tracked worker task. Cancellation:

- marks the provider inactive;
- clears buffered text;
- signals native generation to stop at the next yielded chunk;
- invalidates queued audio;
- wakes any waiter blocked on the bounded queue; and
- waits up to one second for the tracked worker to stop.

No post-cancel audio is intentionally forwarded. Unit coverage uses a cooperative blocking fake runtime to prove that cancellation wakes the consumer, stops generation and suppresses late audio.

Pocket's underlying Python/native inference thread is not force-killable by the interpreter if the vendor code itself stops yielding. The implementation fails closed and logs if the tracked worker does not return inside the bounded one-second window. Real-device barge-in/cancellation evidence is therefore still required before treating this provider as live-call accepted.

## Fallback semantics

The ElevenLabs+Pocket wrapper preserves the same bounded pre-audio recovery contract previously proven for the eSpeak fallback:

- every primary `send()` is forwarded immediately;
- before first primary audio, a shadow recovery copy is retained up to 4096 characters;
- that copy never gates primary streaming;
- overflow clears/disables same-turn replay instead of growing without bound;
- if ElevenLabs fails before first audio, the unheard shadow text may be replayed through already-warmed Pocket;
- once any ElevenLabs audio has been emitted, replay is permanently disabled for that turn;
- a mid-answer ElevenLabs failure therefore never restarts the whole answer in another voice.

`tests/test_tts_streaming_contract.py` pins the immediate-forwarding and bounded-shadow invariants independently of the concrete local fallback implementation.

## Provider-selection invariants

- default provider remains ElevenLabs;
- Pocket is explicit/optional and does not change default startup behavior;
- `TTS_PROVIDER=pocket` never constructs ElevenLabs;
- configured Pocket fallback with a missing ElevenLabs key bypasses ElevenLabs entirely;
- missing optional Pocket package fails clearly before the turn when Pocket is selected;
- no state-machine event/action was added;
- `process_event` remains pure;
- TTSPool contract and existing ElevenLabs monkeypatch seam remain intact;
- Pocket SDK import is lazy and stays inside the provider module;
- no secret is logged or inspected;
- no raw audio recording is introduced;
- this is not Phase 6 call control.

## Automated validation commands

The focused gate for this replacement is:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
python -m pytest -q \
  tests/test_tts_pocket.py \
  tests/test_tts_provider.py \
  tests/test_tts_streaming_contract.py \
  tests/test_tts_failure.py \
  tests/test_bluetooth_production.py \
  tests/test_bluetooth_conversation.py \
  tests/test_player.py \
  -p no:cacheprovider
```

Then:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
python -m pytest -q tests/test_bluetooth_*.py -p no:cacheprovider
```

Then the complete repository suite:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
python -m pytest -q -p no:cacheprovider
```

The complete-suite gate remains baseline-identity based. The only tolerated historical failures are:

- `scripts/test_v2_keys.py::test_shunya_key` — unsupported unmarked async test;
- `scripts/test_v2_keys.py::test_azure_key` — unsupported unmarked async test;
- `tests/test_config_api.py::TestIsolation::test_the_call_server_has_no_config_routes` — `_IncludedRouter.path` AttributeError;
- `tests/test_test_call.py::TestTheProcessSplitSurvives::test_the_call_server_has_no_test_call_routes` — same `_IncludedRouter.path` AttributeError.

Any new failure identity/signature is a regression and must be fixed before merge.

## Historical eSpeak record

eSpeak was introduced and automated-regression validated earlier on 2026-09-16. Its final gate proved real subprocess/WAV/mu-law conversion, direct/fallback routing, 99 focused passes, 162 Bluetooth passes and a full-suite baseline-clean result. That evidence remains historical and is not rewritten as a failure. The replacement decision is about practical intelligibility for the remaining functional Phase 5 work, backed by the later A/B measurements above.

The eSpeak runtime provider file and routing options are removed by this replacement; historical issue/doc evidence remains preserved.

## Acceptance boundary

Automated provider regression, even when baseline-clean, proves only code-path, codec-boundary, streaming, boundedness and regression behavior. It does **not** prove:

- caller-heard Pocket latency over Bluetooth/cellular;
- reference-call clarity after the actual Bluetooth path;
- real barge-in/cancellation under simultaneous STT/LLM/TTS CPU load;
- echo/feedback behavior;
- Phase 5 acceptance; or
- Phase 6 readiness.

Those items require the separately authorized reference-device/manual evidence. Phase 6 remains blocked.
