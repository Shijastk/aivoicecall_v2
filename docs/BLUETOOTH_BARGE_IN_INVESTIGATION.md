# Controlled Bluetooth barge-in investigation — 2026-09-15

## Conclusion and provenance

**The reported audible cut followed by a missing answer is not localized by the
latest log. No behavioral fix is justified from this recording of diagnostics.**
This is a lifecycle investigation, not Phase 4C or Phase 6. No speculation rule,
provider setting, event/action semantics or cancellation order changed.

Reviewed only content-free diagnostic fields from
`/tmp/bt-phase4b1-100ms.log`, the newest Bluetooth live log found and the one
matching the supplied 39.9s/10.4s/7.6s totals. Size: 349,854 bytes; SHA-256:
`3b30cff1fa736984493c8d8b0d8967e4040a66ca1aa6b38ef720678b263dfe5f`.
Times below are the log's clock; no timezone conversion is assumed. No transcript,
recording, credentials or private call-history data was extracted. Source was
inspected in the existing dirty worktree, preserving its earlier local edits.

The file contains **9 StartOfTurn, 4 TurnResumed, 9 final EndOfTurn, 9 normal Agent
starts, 9 TTS first-audio and 9 playback-dispatched milestones; zero normal Agent
cancellation milestones**. All four resumes precede final EOT, after the preceding
response has already dispatched. No logged StartOfTurn or TurnResumed falls inside
an Agent response interval. This does not disprove the user's audible experience;
it means the log does not capture the assumed normal barge-in lifecycle.

## Ordered live trace

Rows are normal Agent responses, not speculative generations. Line references
refer to the original log; the final-EOT line is the Bluetooth callback receipt.

| Turn | StartOfTurn | TurnResumed | Final EOT (line) | Agent start delay | TTS first audio | Playback dispatched (line) |
|---|---|---|---|---|---|---|
| 1 | 13:00:27.474 | — | 13:00:27.722 (44) | 0.8ms | 13:00:29.050 | 13:00:30.732 (160) |
| 2 | 13:00:33.657 | — | 13:00:33.879 (211) | 0.7ms | 13:00:34.628 | 13:00:38.909 (297) |
| 3 | 13:00:40.584 | 13:00:40.789 | 13:00:40.915 (337) | 0.1ms | 13:00:42.133 | 13:00:44.334 (414) |
| 4 | 13:00:49.277 | — | 13:00:49.660 (489) | 0.2ms | 13:00:50.682 | 13:00:51.943 (551) |
| 5 | 13:00:59.109 | — | 13:01:00.638 (673) | 0.1ms | 13:01:02.980 | 13:01:40.601 (1203) |
| 6 | 13:01:51.705 | 13:01:53.570 | 13:01:53.776 (1385) | 0.1ms | 13:01:54.705 | 13:02:04.146 (1607) |
| 7 | 13:02:09.826 | 13:02:10.226 | 13:02:11.550 (1737) | 0.1ms | 13:02:12.264 | 13:02:19.145 (1865) |
| 8 | 13:02:24.338 | 13:02:25.005 | 13:02:25.530 (1956) | 0.1ms | 13:02:26.723 | 13:02:31.944 (2073) |
| 9 | 13:02:33.615 | — | 13:02:35.466 (2126) | 0.1ms | 13:02:36.225 | 13:02:40.685 (2218) |

The three highlighted totals are **39,963ms, 10,369ms, 7,594ms** measured from
Agent start to final local dispatch, not TTS first-byte latency or evidence of
stuck cancellation. Between their EOT receipt and dispatch, respectively **170,
70, 39** Update receipts reach shadow admission with `reason=turn_closed`.
No new final EOT occurs within those response intervals. No `Playback failed`,
`Message handling failed`, `TTS produced no audio`, `Receive failed`, `Send failed`,
Flux mid-call-close or fatal-error marker was found. Absence of these markers
cannot rule out an unobserved downstream stall or audible cut.

## Source lifecycle and hypotheses

1. **Caller signal:** `FluxService._on_message` maps StartOfTurn to the Bluetooth
   callback, which queues `FluxStartOfTurnEvent`. `process_event` changes
   RESPONDING to LISTENING and emits `ResetAgentTurnAction`. TurnResumed instead
   reaches `SpeculativeTurnCoordinator.on_resumed`; it does not queue a normal
   interruption event. The actual four resumes occurred before a normal answer
   was started. A resume-only interruption during RESPONDING would not cancel
   the normal Agent in the current code; this is a source-level gap to observe,
   not the demonstrated cause of this reported cut.
2. **Cancellation:** Bluetooth dispatch awaits `Agent.cancel_turn`. It sets
   `_active=False`, then awaits LLM cancel, TTS cancel and player stop/clear in
   order. `TTSService._cleanup` awaits receiver cancellation and WebSocket close.
   A slow close can delay player clear and event dispatch. There is no cancellation
   in this log from which to measure such a delay. No cancellation reorder or
   timeout was introduced on this unproven hypothesis.
3. **Updates:** `SpeculativeTurnCoordinator.on_final` sets `_turn_open=False`.
   Updates then report `turn_closed`; only `on_start` opens a new shadow turn.
   `on_resumed` invalidates tentative work/stability without opening a finalized
   turn. This state is independent of `AppState.phase`. Updates do not start the
   normal Agent. Synthetic tests verify repeated resumes/corrections inside the
   next started utterance do not prevent its final normal answer.
4. **Final EOT:** Bluetooth always queues `FluxEndOfTurnEvent`, after synchronous
   shadow finalization. `process_event` starts a nonempty final only while
   LISTENING; it ignores finals while RESPONDING. There is no evidence of an
   ignored final here: all nine led to starts.
5. **Normal restart:** `Agent.start_turn` checks out TTS, creates a fresh
   `AudioPlayer`, and starts its persistent LLM. Shadow capacity/task ownership
   is separate; normal Agent execution never acquires the shadow gate. Tests
   hold cancelled shadow work open until after the normal answer completes.
6. **TTS first audio:** `_on_llm_token` streams to TTS; `_on_tts_audio` streams to
   player. All nine live turns have first audio. Tests inject only providers,
   exercising these real Agent callbacks without network access.
7. **Playback:** `AudioPlayer.stop_and_clear` cancels/awaits its playback task,
   discards its buffer and clears Bluetooth media. `BluetoothOutboundMedia`
   converts only at the isolated outbound boundary; session clear reaches
   `PwCatPlaybackEndpoint.clear`, which clears its bounded queue and pending
   pre-roll. Already dequeued/in-flight writes, OS/pw-cat/PipeWire/phone buffers
   are not retracted. Dispatch completion acknowledges none of those downstream
   layers. Stale downstream audio ownership and actual handset delivery remain
   unknown in this log and in the hardware-free tests.

## Smallest safe change applied

Content-free observability and regression coverage, with existing behavior intact:

- Bluetooth `BTLifecycle` logs Update counts/character counts/current phase,
  resume receipt and its lack of a normal action, dequeued event transitions and
  action types, event queue depth, numbered Agent start/cancel begin/return and
  completion receipt. Phase at callback receipt is a snapshot and can precede
  consumption of already queued events; use the dequeued transitions for state.
- Shared Agent logs cancellation stage boundaries and adds its trace turn number
  to TTS first-audio and playback completion. Shared carrier/browser behavior is
  unchanged; these content-free lifecycle log additions also appear there.
- Bluetooth outbound logs first session write begin/return, clear begin/return
  with duration, and dispatch chunk totals. A returned write means local queue
  acceptance, **not handset playback**. No per-audio-frame log was added.
- The developer log filter now includes lifecycle and playback-dispatch entries.
  Tests assert that synthetic transcript/answer text never appears in diagnostics.

No new task/queue, dependency, speculative promotion, threshold revision, carrier
codec change, device action, call control, commit or push was added. Historical
planning-only scope in REQUIREMENTS and older Phase 4 status wording do not
supersede the task owner's explicit authorization for this investigation/tests;
those historical restrictions were not rewritten to claim compliance.

## Next controlled test

**Justified, subject to a separately authorized manual live run. None was started.**
Use the existing shadow-only setup and current thresholds. Record an operator
clock marker when speech interrupts an audible long answer, without recording
what was said. Retain the complete content-free lifecycle stream and compare:

1. StartOfTurn/TurnResumed receipt while RESPONDING and its dequeued action;
2. LLM/TTS/player cancellation boundaries and Bluetooth clear return;
3. post-interruption Updates, their phase, and final EndOfTurn;
4. exactly one subsequent Agent start, TTS first audio and first write/dispatch;
5. manual observation that the new answer is audible at the phone.

If no caller turn boundary/final arrives, investigate capture/provider turn
recognition using separately scoped evidence. If a final is ignored while
RESPONDING after resume-only signaling, the Bluetooth coordination mapping is
the next bounded fix to evaluate. If cancel stalls, its last stage identifies
where to investigate. If local dispatch succeeds but the caller hears silence,
measure the PipeWire/phone output boundary before claiming normal Agent failure.
See [TESTING](TESTING.md#bluetooth-barge-in-lifecycle-investigation--2026-09-15)
for offline results and their limits. Phase 4C remains deferred.
