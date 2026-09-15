# Phase 4 controlled live validation — 2026-09-15

## Status

**Repository integration validated through STT, turn handling, Agent startup and LLM generation on the reference Bluetooth cellular path. Final caller-heard TTS/audio acceptance remains pending because the ElevenLabs account quota was exhausted during the controlled run.**

This document records task-owner supplied live evidence from 2026-09-15. It does not authorize Phase 5 or later phases and does not claim a final caller mouth-to-ear latency result.

Reference phone: `00:C7:11:7B:84:21`.

## 1. Initial full-run failure and Bluetooth baseline recovery

An initial controlled Phase 4D smoke run forwarded Bluetooth audio bytes to Flux but produced no `StartOfTurn` or final `EndOfTurn`. During the same period, ordinary Bluetooth HFP calling also became corrupted: the laptop could not reliably hear the remote caller and the remote caller heard beep/crackle/corrupted audio.

PipeWire/WirePlumber restart alone did not resolve the audible baseline problem. A full Bluetooth recovery was then performed: the phone was disconnected, stale SCO/ACL state disappeared, the controller was powered off/on, and the phone was manually reconnected. A fresh HFP session then showed active eSCO traffic, recreated SCO nodes, mSBC / S16LE / 16 kHz mono, baseline physical links restored, no new kernel Bluetooth errors in the sample, and both call directions were audibly confirmed working.

The exact root cause of the earlier corrupted SCO session is **not proven**. The recovery is consistent with stale/corrupted HFP/SCO state, but this document does not promote that observation to a confirmed root cause.

## 2. Raw Bluetooth downlink boundary

With SHUO off and a normal cellular call active, a five-second direct `pw-cat` capture from the discovered `api.bluez5.sco.source` produced:

- bytes: `159744`
- samples: `79872`
- audio duration: `4.99 s`
- RMS: `1435.3`
- peak: `13905`
- near-silence samples below the diagnostic threshold: `71.7%`
- `pw-cat` stderr: empty

This established that the caller signal was present at the raw PipeWire Bluetooth PCM boundary.

## 3. Bluetooth codec boundary

The same live input was passed through the production `BluetoothInboundCodec` and the resulting mu-law was decoded only for content-free signal-level comparison.

Observed values:

| Metric | Raw S16LE 16 kHz | Decoded mu-law 8 kHz |
|---|---:|---:|
| Duration | 4.99 s | 4.99 s |
| RMS | 3128.9 | 3130.6 |
| Peak | 32767 | 32124 |

The production Bluetooth boundary conversion therefore preserved the observed signal energy and duration. No codec corruption was indicated by this test.

## 4. Direct codec-to-Flux validation

A minimal live path was tested without Agent, Groq, ElevenLabs or player output:

`Bluetooth SCO -> pw-cat -> BluetoothInboundCodec -> Deepgram Flux`

Observed callbacks included:

- transcript updates
- `StartOfTurn`
- `EndOfTurn`

The final diagnostic reported approximately `380928` PCM bytes and `95232` mu-law bytes, with an empty `pw-cat` stderr. This directly validated the production audio format and Flux configuration on the recovered Bluetooth session.

## 5. Phase 3 AI-only session isolation

The production Phase 3 AI-only session was started independently of the full conversation pipeline.

It removed the expected four physical routing links while active:

- two physical microphone -> Bluetooth uplink links
- two Bluetooth downlink -> physical speaker links

During the eight-second capture window:

- bytes: `258048`
- audio duration: `8.06 s`
- RMS: `1083.7`
- peak: `9277`
- near-silence: `47.0%`

The remote caller reported no beep/crackle in this run, and the previous echo was also absent. Shutdown reported `session.stop(): OK` and `Pending unrestored links: 0`.

This run did not support the hypothesis that AI-only route isolation or an idle playback process was inherently responsible for the earlier audible corruption.

## 6. Full SHUO pipeline through LLM

After Bluetooth baseline recovery, the original full SHUO smoke configuration was rerun with:

- `--latency 120ms`
- `--eot-threshold 0.5`
- `--llm-provider-timing`
- `--parallel-startup`

The live path successfully produced:

- Flux connection and first caller audio forwarding
- repeated `StartOfTurn`
- repeated final `EndOfTurn`
- `StartAgentTurnAction`
- Agent startup
- Groq/Qwen streamed responses
- barge-in/cancel handling with history preservation

Examples from the run:

- Flux final EOT -> Agent start: approximately `0.6–0.8 ms`
- observed LLM first-token times included approximately `1369 ms`, `1187 ms`, and `426 ms`

The only downstream generation failure in that run was ElevenLabs account quota exhaustion. ElevenLabs returned `quota_exceeded`; the Agent correctly logged that TTS produced no audio and that the caller heard silence.

Therefore this run validates the live pipeline **through LLM generation**, but not final caller-heard synthesized audio.

## 7. Phase 4C prepared-response reuse live result

A controlled Phase 4C run enabled eager EOT, shadow speculation, early transcript admission and prepared-response reuse while retaining final-EOT correctness fallback.

Summary:

- `StartOfTurn`: 9
- `EndOfTurn`: 9
- Agent starts: 9
- Agent cancels: 2
- prepared-response reuse promotions: **0**
- quota errors: 9

Observed speculative outcomes included `not_ready_by_final`, `TurnResumed` cancellation and final transcript mismatch discard. Representative eager-to-final windows included approximately `333 ms`, `42.7 ms`, `1.1 ms`, and `0.9 ms`. The shadow result was not first-token-ready before final EOT in the observed reusable candidates.

Interpretation:

- correctness/fallback behavior worked
- stale/retracted candidates were not promoted
- no prepared response was reused in this live sample
- no latency win from prepared-response reuse is claimed

This is a valid negative performance result, not a correctness failure.

## 8. Phase 4D parallel startup A/B

Controlled startup measurements:

| Mode | Startup ready |
|---|---:|
| Serial | `1809.9 ms` |
| Parallel | `801.5 ms` |

Observed saving: `1008.4 ms`, approximately `55.7%` lower startup wall-clock time in this A/B sample.

Parallel startup therefore has direct live evidence of meaningful startup improvement on the tested path.

## 9. Deferred tests

The following tests remain intentionally deferred until ElevenLabs credits are available again:

- final caller-heard end-to-end synthesized audio validation
- caller-heard TTS latency and audio quality
- phrase grouping / `tts_phrase_chars` live A/B
- player pre-roll 2-vs-3-frame live A/B
- any mouth-to-ear latency claim
- long-history live A/B where the operator needs to hear AI turn completion to avoid contaminating measurements with premature barge-in

## 10. Current acceptance boundary

The evidence supports the following statements:

- Bluetooth/PipeWire downlink capture: validated on the recovered reference session
- Bluetooth PCM <-> core mu-law boundary: validated
- Deepgram Flux turn detection: validated
- Phase 3 AI-only route isolation and tested cleanup: validated in the controlled run
- SHUO turn state -> Agent -> Groq path: validated
- barge-in/cancel path: observed working
- Phase 4C safety fallback: observed working; no reuse/latency gain observed
- Phase 4D parallel startup: live A/B improvement observed
- ElevenLabs final synthesis/caller playback: **pending due account quota**

Merging the Phase 4C/4D implementation does **not** mean Phase 4 final runtime acceptance is complete. Final acceptance remains gated on the deferred TTS-dependent controlled tests above. Phase 5 is not advanced by this merge.
