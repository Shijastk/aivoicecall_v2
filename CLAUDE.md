# CLAUDE.md — shuo

> **Naming note:** `.claude/` in this repo is a *directory* holding `settings.json` (Entire's hooks — do not edit). `CLAUDE.md` is the file Claude Code auto-loads into context every session. This file is the "`.claude` file" referred to in project conversations.

**Read order at session start:** this file → [context.md](context.md) (where we are) → [rules.md](rules.md) (how to build) → [plan.md](plan.md) (deep research, *read the corrections below first*).

---

## 1. What we are building

A **multi-persona, emotionally intelligent voice Digital Twin** for Indian telephony — Indian-accented English over a standard 10-digit Vobiz DID, running the existing `shuo` streaming loop with sub-second turn-taking.

The persona is **runtime configuration, not code**. One pipeline, many roles, selected at call setup:

| Phase | Persona | Direction | Status |
|---|---|---|---|
| **Test Phase 1** | **JOB CANDIDATE being interviewed** | outbound | **current focus** |
| Future | Technical Interviewer | outbound | planned |
| Future | HR Recruiter | outbound | planned |
| Future | Inbound Receptionist | **inbound** | planned |

### Why "job candidate" first

It is deliberately the **hardest** persona, chosen as an adversarial stress test. A candidate does not control the conversation — the human does. That maximally exercises the four failure modes we are hunting:

1. **Character break** — drops the persona, says "as an AI language model", answers meta-questions about itself.
2. **Hallucination** — invents résumé facts, employers, dates, salary figures not in its grounded profile.
3. **Context loss** — forgets what it said three turns ago; contradicts an earlier answer under follow-up.
4. **Robotic delivery** — uniform response gaps, no hesitation, no self-correction, over-structured "firstly/secondly" answers.

If the pipeline survives an unscripted interview, every other persona is easier.

---

## 2. 🔴 CORRECTIONS TO `plan.md` — these override the document

`plan.md` was researched against a *promotional telecalling* brief. That brief is **wrong**. When `plan.md` conflicts with this section, **this section wins**.

### 2.1 We are NOT building a promotional dialer

**Ignore entirely:**

- **§0.1** in full — the 140xx CLI mandate, TCCCPR/TRAI promotional analysis, Reg 4 auto-dialer declaration, Reg 25 sanction ladder, abandoned/silent-call ratio caps, the 10:00–21:00 IST regulatory calling window.
- **DLT registration** — Principal Entity registration, physical verification, biometric auth, content/consent templates, VILPOWER fees. All out of scope.
- **DND scrubbing** — no pre-dial Scrubbing Function, no tokenised virtual identities, no CCAF/OTP consent capture.
- **Phase 0** (compliance & procurement) — deleted, except one surviving item: confirm Vobiz can provision a working **standard 10-digit Indian DID** and give us trunk credentials.
- **Phase 8** (dialer & compliance gates) — deleted. No calling-window gate, no abandoned-call counters, no scrub-validity checks. A thin call-outcome classifier from SIP response codes may return later as plain observability, not compliance.
- **Part 7 → "Compliance"** verification block — deleted.

**We use a standard 10-digit Vobiz DID.** Calls are consented, relationship-based, low-volume, one-to-one (interviews, reception, recruiting conversations) — not cold promotional broadcast.

**Still true and still load-bearing** from §0.1: there is no Indian rule requiring AI-caller disclosure, and DPDP 2023 permits US-hosted processing. So **ap-south-1 remains a pure latency decision.**

### 2.2 It is a Digital Twin, not a receptionist

`plan.md` assumes a single fixed outbound sales script. Replace that assumption with:

- **Persona is data.** A persona = system prompt + grounded fact block + voice ID + turn-taking profile + filler inventory. Swappable without touching the loop.
- **Inbound is a first-class path**, not an afterthought — the Receptionist persona needs it. `plan.md` Phase 1 only specifies outbound origination; the `<Stream>` answer-URL must serve both.
- **§7 "Replace the placeholder SYSTEM_PROMPT with a telecalling script"** → replace with the **persona layer** (`shuo/persona/`).
- **Emotional intelligence is a requirement, not polish.** Part 5 (fillers, response-gap sampling, backchannel suppression) is promoted from "human-likeness nice-to-have" to core acceptance criteria.

### 2.3 What survives from `plan.md` unchanged

Everything technical. Specifically: the re-specified latency targets (§0.2), Vobiz transport and its trap list (§2.1), Sarvam `saaras:v3` STT (§2.2), in-process Silero VAD v5 + Smart Turn v3.1 (§2.3), the LLM decision-by-measurement (§2.4), Cartesia Sonic 3.5 + voice cloning (§2.5), the cost model (Part 3), the latency budget and Bugs A/B/C (Parts 1.3 & 4), human-likeness (Part 5), and Phases 1–7 + 9.

---

## 3. Non-negotiable rules

1. **The state machine stays pure.** [shuo/state.py](shuo/state.py) `process_event` is `(State, Event) -> (State, [Action])` with zero I/O. Every new capability enters as new **events** and **actions**, never as a side effect inside the machine. All 307 lines of [tests/test_update.py](tests/test_update.py) must keep passing.
2. **Never break the streaming chain.** Token-level LLM → TTS → Player streaming ([shuo/agent.py:158-193](shuo/agent.py#L158-L193)) is the entire latency advantage. No buffering a full response anywhere.
3. **Vendor code goes behind a provider interface.** No vendor SDK imported outside its provider module. Swapping Cartesia↔ElevenLabs↔Inworld must be a config change.
4. **Measure, don't trust.** Every vendor latency claim gets benchmarked from ap-south-1 before it earns a place in the pipeline. [shuo/server.py:200-283](shuo/server.py#L200-L283) and [scripts/bench_sarvam.py](scripts/bench_sarvam.py) exist for this.
5. **µ-law 8kHz end to end.** No L16 path may be reachable — see the endianness trap in [rules.md](rules.md).
6. **Ask before scope-expanding.** If work implies a change to the architecture recorded in [context.md](context.md), stop and confirm.
7. **Do not write Phase code before the phase is approved.** Plan → approval → code.

---

## 4. Persona rules (Digital Twin)

1. **Grounded facts only.** Each persona carries an explicit fact block (for the candidate: résumé, skills, dates, employers, expected CTC, notice period). The prompt must forbid inventing anything outside it, and must give an explicit *"I don't have that detail"* escape hatch — hallucination is worse than a gap.
2. **Never break character.** No meta-references to being an AI, a model, or a system prompt. Deflect identity probes in-persona.
3. **Register: Indian English.** Natural Hinglish code-mixing where a real speaker would use it (`ji`, `haan`, `theek hai`, `matlab`, `achha`). Lakh/crore, Indian number grouping.
4. **Short conversational turns.** The candidate persona is the exception — it must sustain 20–45s answers without becoming a monologue. That makes barge-in handling and played-ms truncation (Bug A) *critical*, not optional.
5. **Turn-taking profile is per persona.** An interviewer pausing mid-question ("So… tell me about… your Python experience") must **not** trigger a premature end-of-turn. The candidate persona needs a *patient* EOT threshold; the receptionist needs an eager one.

---

## 5. Working agreements

- **Update [context.md](context.md) at every milestone** — phase completion, architectural decision, vendor confirmed or rejected, benchmark result. It is the recovery point if conversation context is lost.
- Record decisions in the context.md **Decision Log** with date, choice, and reason. Reversals get a new entry, not an edit.
- Never delete a `plan.md` section — it is the research archive. Corrections live here.
- Dev machine is **Windows**; deploy target is **Linux ap-south-1**. Do not introduce POSIX-only paths (see Bug C).

---

## 6. Commands

```bash
python -m pytest tests/ -v            # state machine — must stay green
python main.py                        # server-only (inbound)
python main.py +91XXXXXXXXXX          # outbound call
python scripts/bench_sarvam.py        # full-pipeline latency, no telephony needed
curl localhost:3040/bench/ttft        # LLM TTFT comparison
```
