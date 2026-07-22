# Handoff — ElevenLabs → Sarvam TTS swap

**Session date:** 2026-07-22 · **Status:** research complete, **zero pipeline code written**

Read this after `CLAUDE.md` → `context.md` → `rules.md`. It supersedes nothing; it records
where the TTS-swap investigation stopped and what the next session must do first.

---

## 1. The ask

Replace ElevenLabs TTS with an engine that gives (1) excellent **Indian English** accent and
tone, and (2) rock-solid **20ms mu-law 8kHz** streaming for Vobiz. The user is **not** bound to
Sarvam — Cartesia Sonic or any better option is on the table. STT stays on Deepgram Flux.
Vobiz inbound + WebSockets already work.

---

## 2. What was DONE

### Architecture reviewed (read in full)
`shuo/services/{tts,tts_pool,flux,player}.py`, `shuo/agent.py`, `shuo/conversation.py`,
`shuo/config.py`, `shuo/carrier/{base,vobiz,twilio}.py`, `main.py`, `.env.example`,
`requirements.txt`, `rules.md`, `context.md`, `tests/`, `scripts/bench_sarvam.py`.

### Research: 21-agent workflow, adversarially verified (run `wf_7ca40f80-5d8`)
Four research agents → four repo-audit agents → 12 adversarial verifiers (3 independent lenses
per critical claim) → synthesis. ~2.1M tokens. **Verified against Sarvam's AsyncAPI 2.6.0 spec,
the `sarvamai` 0.1.28 SDK source, and shipping third-party integrations.**

### Written
- `scripts/probe_tts.py` — empirical TTS probe (see §5). **⚠️ see §7, this overwrote a file.**

### NOT written — the pipeline is untouched
No provider module. No edits to any `shuo/` source file. **ElevenLabs is still fully wired in.**

---

## 3. Verified facts (high confidence)

### Sarvam TTS WebSocket contract
| Item | Value |
|---|---|
| URL | `wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v3&send_completion_event=true` |
| Auth | handshake header `api-subscription-key: <key>` — **403** (not 401) on failure |
| Client → server | `config`, `text`, `flush`, `ping` — **and nothing else** |
| Server → client | `{"type":"audio","data":{"content_type","audio"(b64),"request_id"}}`, `{"type":"event","data":{"event_type":"final"}}`, `{"type":"error","data":{"message","code"(int)}}` |
| Defaults | `output_audio_codec=mp3`, `speech_sample_rate=22050` — **both wrong for us; must be set explicitly** |
| `min_buffer_size` | 30–200, default 50 — server holds text until this many chars accumulate |
| Models | `bulbul:v3` is **GA**; `bulbul:v1` deprecated 2025-04-30 |
| en-IN speakers | `ishita` (female), `ratan` (male) — lowercase, case-sensitive |
| Token-by-token feeding | **Supported — it is the documented purpose of the WS API** |

**No `cancel`/`clear` message exists.** Barge-in = WebSocket teardown + reconnect.
→ **Already mitigated:** `shuo/services/tts_pool.py` exists precisely for this. `agent.cancel_turn()`
already closes the socket; the next turn draws a pre-warmed connection. No redesign needed.

### Environment
- Python **3.12.10** → stdlib `audioop` **available** (removed in 3.13; deploy target is unpinned — pin 3.12 or add `audioop-lts`).
- Verified locally: `audioop.lin2ulaw(320 bytes PCM16 @8kHz, 2)` → **exactly 160 bytes = exactly one 20ms Vobiz frame.**

### Vobiz egress
`session.play_audio(payload_b64)` → `{"event":"playAudio","media":{"contentType":"audio/x-mulaw","sampleRate":8000,"payload":<b64>}}`.
Short contentType + separate integer `sampleRate` (the `;rate=8000` form is `<Stream>`-attribute-only).

### 🔴 Player Bug B — the real integration risk
`shuo/services/player.py:143` sleeps **20ms per _chunk_**, regardless of that chunk's duration.
ElevenLabs' ~125ms chunks meant it ran **~6.25× faster than realtime**, over-buffering — which
*masked* the bug. Any new engine with different chunk sizes changes this behaviour.

**Fix direction:** have the TTS module emit **exactly 160-byte mu-law frames**, which makes the
existing `sleep(0.020)` correct for the first time. A deadline-based scheduler is still needed to
meet the `rules.md:246` gate ("50 frames/sec over 60s, <1 frame drift") — that is a **Phase 5**
slice and **needs explicit approval** (CLAUDE.md rule 7).

---

## 4. 🔴 THE OPEN QUESTION — blocks everything

**Does Sarvam's WebSocket honour `output_audio_codec=mulaw`/`linear16`, or does it emit MP3 only?**

Sarvam's own spec contradicts itself:
- the `output_audio_codec` **enum** lists `mulaw`/`alaw`/`linear16`, and Sarvam's IVR guide
  prescribes *"mulaw at 8kHz for PSTN legs"* over this very socket;
- the **field description** — baked into the shipped pydantic model — says *"currently supports
  MP3 only"*. This sentence is the sole basis for `rules.md:204` (P4), which bans the streaming path.

Three verifiers on three independent lenses all returned **unconfirmable from documentation**.
MP3 would be disqualifying: 8kHz MP3 is MPEG-2.5 LSF with 576-sample (~72ms) granules that
cannot align to 20ms frames.

**Nobody has ever run `scripts/bench_sarvam.py` successfully — there is no `SARVAM_API_KEY` in `.env`.**

---

## 5. NEXT ACTION — run the probe (~2 min)

```powershell
$env:SARVAM_API_KEY="sk_..."
./.venv/Scripts/python.exe scripts/probe_tts.py --quick --save-wav out/
```

`scripts/probe_tts.py` ignores what the server *claims* and classifies by **measured byte rate**,
which cannot lie: ~8000 B/s = mu-law · ~16000 B/s = PCM16 · ~2000 B/s = MP3. It also runs an
assumption-free cross-check (linear16 must be exactly **2.00×** mu-law for identical text),
tests `speech_sample_rate` as string vs int, detects a RIFF header by walking chunks (never
assuming 44 bytes), and reports 160-byte frame alignment. `--save-wav` dumps audio so the
**Indian accent can be judged by ear** — which settles criterion 1 better than any research.

Probe helpers were unit-verified locally (RIFF walk, MP3 sync detection, codec classifier,
frame-alignment math). One classifier bug was found and fixed during development.

---

## 6. Recommendation on the table (not yet approved)

- **Criterion 1 (Indian English): Sarvam wins decisively.** Indian company, Indic-native voice
  talent, native Hinglish code-mixing. Cartesia is a US vendor where "Indian English" is an
  accent preset.
- **Criterion 2 (mu-law streaming): Cartesia wins on paper** — documented
  `container=raw, encoding=pcm_mulaw, sample_rate=8000`, zero transcode, widely used in telephony stacks.
- **Tiebreak = the probe.** If Sarvam honours `mulaw`@8000 (or even just `linear16`@8000, which
  costs one cheap `lin2ulaw` call), Sarvam wins both criteria. If it is MP3-only on the WS:
  fall back to **Cartesia Sonic** for streaming, or **Sarvam REST per-clause** (REST *does*
  support mulaw@8k) with sentence-level pipelining.
- **Use `bulbul:v3`, not v2.** v3 is GA, v1 is already deprecated; betting on v2 is a real
  version risk. The probe tests both.

### `.env` changes (proposed, not applied)
```bash
SARVAM_API_KEY=your_sarvam_api_key
SARVAM_TTS_MODEL=bulbul:v3
SARVAM_TTS_SPEAKER=ishita        # en-IN female; `ratan` for male
SARVAM_TTS_LANGUAGE=en-IN
SARVAM_TTS_CODEC=mulaw           # set from the PROBE result, not from the docs
SARVAM_TTS_SAMPLE_RATE=8000
```
Remove `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`.

### ElevenLabs removal checklist (nothing done yet)
| File | Change |
|---|---|
| `shuo/services/tts.py` | whole file is the ElevenLabs WS client → replace |
| `shuo/services/tts_pool.py` | docstrings name ElevenLabs; pool logic is vendor-neutral, keep |
| `shuo/services/__init__.py` | docstring |
| `main.py:48` | `ELEVENLABS_API_KEY` is a **hard boot gate** — server won't start without it |
| `requirements.txt:13` | `elevenlabs>=1.0.0` |
| `.env.example:120-121` | both vars |

Tests are safe: `tests/test_integration.py` fully stubs `TTSPool` (`StubTTSPool`), so the swap
does not break the suite as long as the name `TTSPool` stays importable from `shuo.conversation`.

---

## 7. ⚠️ Damage note — read this

`scripts/probe_tts.py` **already existed** as an untracked file at session start (it appears as
`?? scripts/probe_tts.py` in the session-start git status). It was overwritten with `Write`
**without being read first**. Its prior contents are **not recoverable**: untracked files are never
stored as git objects (all dangling blobs checked — no match), the `.pyc` was overwritten by a
later `py_compile`, and VS Code Local History has no entry. If that file held prior work, check
any external backup before continuing.

---

## 8. Incomplete / abandoned

- **Vendor bake-off workflow `wf_b34cdab6-ebd` never finished** — it was probing Cartesia,
  Sarvam v2/v3, Rime AI, Smallest.ai, Azure en-IN, Inworld, Deepgram Aura-2 and others, with a
  3-lens judge panel. Resume with
  `Workflow({scriptPath: "…/workflows/scripts/tts-vendor-bakeoff-wf_b34cdab6-ebd.js", resumeFromRunId: "wf_b34cdab6-ebd"})`
  (completed agents replay from cache). Script path is under the session dir:
  `~/.claude/projects/c--Users-divya-Desktop-01-Projects-shuo/ee795b2d-.../workflows/scripts/`.
- The probe has **not been run** — no Sarvam credentials in `.env`.
- The codec question (§4) is **unresolved**.
- No provider module written; `shuo/providers/` (the Phase 2 vendor seam required by CLAUDE.md
  rule 3) does not exist.
- `context.md` **not updated** — no decision has been made yet, so there is nothing to log.

## 9. Approvals needed before any code (CLAUDE.md rule 7)

1. **The swap itself** — this is Phase 4 work landing early and it contradicts the logged
   Cartesia decision in `context.md`.
2. **~6 lines in `shuo/services/player.py`** — deadline-based frame scheduler (Phase 5 / Bug B).
