# Bluetooth Phase 3 — PipeWire capture/playback integration

**Status: COMPLETE FOR PHASE 3 ON REFERENCE HARDWARE; PHASE 4 PIPELINE INTEGRATION PENDING.**

Shared contracts: [Roadmap](../ROADMAP.md), [requirements](../REQUIREMENTS.md),
[Bluetooth architecture](../BLUETOOTH_ARCHITECTURE.md),
[current architecture](../CURRENT_ARCHITECTURE.md), [testing](../TESTING.md).
Future phase scope and gates are PROPOSED pending review; file names below are
candidate locations unless explicitly identified as existing code.

## Goal and included scope

Prove explicit digital capture and playback with controlled signals before attaching the SHUO conversation pipeline.

Injected subprocess runner implementation; property-based discovery and explicit selector; negotiated-format checks; independent pw-cat capture/playback or reviewed equivalent; fragmented I/O, exit/error handling, bounded stop and route restoration.

## Explicit exclusions

Live SHUO STT/LLM/TTS pipeline, unapproved real calls, automated telephony control and undocumented default routing.

## Dependencies and prerequisites

Phase 2 accepted. Explicit device authorization; versioned environment, known original routes and cleanup procedure. If HFP streams need an active call, obtain separate controlled-call authorization or record that gate blocked; device-only approval is insufficient.

## Files and boundaries

PROPOSED shuo/bluetooth/ discovery and process/stream modules, injected integration tests and opt-in device test harness. No default production entrypoint may import or start the adapter. Linux-specific
PipeWire behavior is permitted only inside the isolated, optional, injected
adapter boundary; core/carrier Windows/Linux portability and Windows development/
carrier startup remain unchanged. Unsupported platforms must fail clearly without
side effects.

## Tests and measurable acceptance gate

Record selected properties/role/profile/format and versions. Feed distinct synthetic uplink/downlink signals; prove targeting, no default-route fallback and correct frame duration/byte order. Exercise missing/ambiguous nodes, process exit, device disappearance and repeated start/stop; no owned processes/handles or stale queues remain. Also verify the injected platform guard rejects unsupported platforms without side effects and preserves default Windows/core/carrier startup. Approve duration and timing budgets before acceptance.

## Risks

Profile visible only during a call; node recreation; ambiguous devices; default fallback; subprocess pipe deadlock or route leakage.

## Rollback

Stop owned capture/playback processes, close pipes, discard queued audio and restore recorded prior links/routes. Confirm no residual device stream before ending authorized session.

## Artifacts and documentation to update

Versioned capability matrix, sanitized property fixtures, device validation procedure/results, process cleanup evidence and updated architecture/decisions.

## Approval and stop boundary

Explicit Phase 4 approval for pipeline seam changes; device approval is not provider/live-call approval.

Stop after the authorized phase, report evidence and unresolved gates. Do not
mark a later phase implemented or start its work from this document alone.

## Phase 3 implementation and reference-hardware evidence — 2026-09-13

Implementation base revision:

```text
97076cd1739e2456843ced239eeebeedcfefa70b
```

That revision added the real PipeWire process/discovery foundation in
`shuo/bluetooth/process.py` and `shuo/bluetooth/pipewire_live.py`.
Subsequent Phase 3 closeout work added AI-only route isolation/session
resources, capture-shutdown cleanup, bounded route-restore retry for transient
BlueZ node recreation, and call-end/device-disappearance handling. The code and
tests are ready to be committed as the final Phase 3 closeout change set.

Reference hardware:

```text
Host: Ubuntu on MSI GF63 Thin 11SC
Phone: itel P40+
Bluetooth address used only as a validation fixture: 00:C7:11:7B:84:21
Profile: HFP Audio Gateway
Codec: mSBC
Bluetooth boundary: S16LE / 16000 Hz / mono
```

Validated real targets during an active cellular call:

```text
Downlink: bluez_input.00_C7_11_7B_84_21.0
Factory: api.bluez5.sco.source
Media class: Stream/Output/Audio

Uplink: bluez_output.00_C7_11_7B_84_21.1
Factory: api.bluez5.sco.sink
Media class: Stream/Input/Audio
```

Numeric PipeWire IDs remain transient and are not production identifiers.

### Route-isolation finding

Live inspection showed WirePlumber/PipeWire automatically linked:

```text
physical laptop microphone -> Bluetooth phone uplink
Bluetooth phone downlink   -> physical laptop speaker
```

The microphone link mixed physical-mic audio into the phone uplink and caused
the previously observed distorted/choppy injected speech. Manual unlinking made
the injected ElevenLabs speech clear.

The implemented AI-only session therefore snapshots and removes only the
relevant physical-mic-to-selected-uplink and selected-downlink-to-physical-speaker
links. Unrelated routes such as Firefox-to-speaker remain untouched. On stop,
only links removed by that session are restored.

### Executed validation

Focused Phase 3/route/cleanup selection:

```text
21 passed, 1 warning
```

Full Bluetooth-focused selection after the cleanup fix:

```text
51 passed, 1 warning
```

The warning is the recorded Python 3.12 `audioop` deprecation warning.

A 20-second real active-call lifecycle test then verified:

- selected physical mic -> Bluetooth uplink links were absent while the session ran;
- selected Bluetooth downlink -> physical speaker links were absent while the session ran;
- explicit `pw-cat --record` and `pw-cat --playback` processes targeted the selected Bluetooth nodes;
- session stop completed without traceback;
- previously removed physical routes were restored;
- `pgrep -a pw-cat` was empty after stop.

A prior hardware run exposed `ProcessError: child process did not exit after
terminate/kill` when capture stdout was left unread. The Phase 3 cleanup fix
drains unread capture stdout during shutdown only; normal capture remains direct
streaming through `read()`. The repeated focused suites and 20-second hardware
lifecycle test passed after that fix.

Additional closeout validation on 2026-09-13:

- five consecutive fresh AI-only session start/stop cycles completed cleanly;
- during an active cycle, explicit `pw-cat --record` and `pw-cat --playback`
  processes targeted the selected Bluetooth nodes;
- after the repeated lifecycle test, `pgrep -a pw-cat` returned no output;
- a real call-cut test reproduced a PipeWire route-restore race while BlueZ HFP
  ports were disappearing/reappearing;
- route restoration was hardened with fresh graph inspection and bounded retry;
- if the selected Bluetooth call port remains absent for the bounded retry
  window, the old route is treated as obsolete because that call stream no
  longer exists;
- real restore failures unrelated to selected Bluetooth-port disappearance
  remain fail-closed and retain ownership for retry;
- focused route/session/process cleanup tests then passed: `25 passed, 1 warning`;
- the real call-cut retest completed with
  `SESSION STOPPED CLEANLY AFTER CALL CUT`;
- after the call-cut retest, `pgrep -a pw-cat` returned no output;
- after hangup, the BlueZ call nodes were absent and only normal local audio
  endpoints remained, which is expected once the HFP call stream is gone.

Latest full-suite regression supplied by the task owner before Phase 3 closeout:

```text
826 passed, 4 failed, 4 warnings
```

The four failures are the same known baseline identities already documented; no
new Bluetooth-related failure identity was introduced.

### What Phase 3 does not yet mean

The 20-second duration belongs only to the validation harness. It is not a
production call limit and is not part of the route-isolation resource itself.

The current default production application does **not** automatically start this
Bluetooth AI-only session when a cellular call begins. The session must next be
wired to the approved runtime/conversation lifecycle so that isolation starts at
Bluetooth AI-session start and remains active until session/call teardown.

Phase 3 does not complete:

- SHUO STT/LLM/TTS conversation integration;
- automated call answer/hangup ownership;
- full remote-disconnect/reconnect lifecycle ownership;
- production end-to-end queue/latency budget approval;
- long-run soak, reconnect, concurrency and coexistence qualification;
- broader device/codec compatibility.

The Phase 3 reference-hardware gate itself is complete. Reconnect/soak/general
resilience remains intentionally assigned to later lifecycle/resilience phases.
