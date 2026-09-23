# Android ADB cellular synthetic-caller TX

**Status:** supplemental Phase-5 development harness implemented from reference-device runtime evidence. It is opt-in, not imported by production entrypoints, and does not alter the carrier/core audio contract.

## Purpose

This harness provides a Vobiz-free synthetic caller transmit path for controlled testing:

```text
Pocket TTS on Ubuntu
-> existing G.711 mu-law / 8 kHz SHUO provider boundary
-> BluetoothOutboundCodec
-> S16LE / 16 kHz / mono
-> USB ADB stdin
-> Android shell app_process
-> AudioTrack TYPE_TELEPHONY
-> active cellular uplink
-> remote handset
```

USB is only the ADB data transport; this is not a bidirectional USB Audio Class sound-card path.

## Reference evidence — 2026-09-23

Task-owner supplied runtime evidence on an itel P683L, Android 13/API 33, Unisoc ums9230:

- vendor policy exposed `voice_tx` to `AUDIO_DEVICE_OUT_TELEPHONY_TX` with in-call-music routing and a mono 16 kHz PCM definition;
- the shell package privapp allowlist contained the required phone/audio-routing permissions and the deny list was empty;
- an app_process probe ran as UID 2000 and found one `AudioDeviceInfo.TYPE_TELEPHONY` output;
- an active call reported `Actual mode = MODE_IN_CALL`;
- the route probe returned `SET_PREFERRED=true` and an actual `ROUTED_TYPE=18`;
- a generated tone, Pocket speech, and then live binary PCM over ADB stdin were heard at the remote Galaxy A10;
- the final live Pocket stream ended with `ROUTED_TYPE=18`, `STREAM_DONE`, and `ADB_EXIT=0`.

A local timing probe measured model-ready at 531.8 ms from process start and first PCM at 630.2 ms, about 98.4 ms between those local boundaries. These are **not** caller-heard, mouth-to-ear, Bluetooth, cellular, or handset latency measurements.

The numeric audio-device id observed in the probe is runtime data and is not encoded as a product constant. The helper discovers by device type and verifies `getRoutedDevice()` before accepting caller PCM.

## Repository implementation

- `tools/android/TelephonyTxBridge.java`: stdin PCM16/16 kHz/mono to the actual telephony route, content-free stderr diagnostics, no file recording, no call control.
- `shuo/benchmark/android_cellular_tx.py`: dev-only preflight/build/push/stream boundary with explicit ADB target selection, call-mode verification, bounded process cleanup, Pocket provider callbacks, and the repository-owned Bluetooth codec.
- `scripts/dev/16_android_cellular_tx.py`: explicit `doctor`, `build`, and one-utterance `speak` commands.
- `tests/test_android_cellular_tx.py`: hardware-free contract, preflight, binary-stream, provider-boundary, and fail-closed route-source coverage.

The production entrypoints do not import this harness.

## Host prerequisites

The reference Ubuntu setup uses ADB, OpenJDK 17, Android API-23 platform stubs, Android build tools, and Debian's `dalvik-exchange`/dx. Default paths are:

```text
/usr/lib/android-sdk/platforms/android-23/android.jar
/usr/lib/android-sdk/build-tools/debian/dx
```

The paths and ADB/javac executables are overrideable with `SHUO_ANDROID_JAR`, `SHUO_ANDROID_DX`, `SHUO_ADB`, and `SHUO_ANDROID_JAVAC`.

Pocket remains optional and follows `requirements-pocket-tts.txt` and `TTS_PROVIDERS.md`.

## Operational boundary

The phone call must already be established and answered manually before the normal doctor/speak path. The harness never dials, answers, or hangs up. For a multi-turn controller, keep one `AdbTelephonyTxBridge` alive for the call and feed turns into it; repeatedly starting app_process adds avoidable startup delay.

The reference runtime consumed all stdin and emitted `STREAM_DONE`, but an earlier probe could remain blocked in Android `AudioTrack.stop()/release()` after Telephony Tx EOF. The validated helper therefore uses its dedicated app_process lifetime as the teardown boundary and the host still has bounded terminate/kill fallback.

## Privacy and interpretation

- no raw-audio persistence;
- no automatic call control;
- no secret inspection;
- no Vobiz dependency for this supplemental path;
- core/carrier audio remains G.711 mu-law / 8 kHz;
- no universal Android-support claim;
- caller-heard latency remains `NOT_MEASURED`.

## Reverse/downlink status

The caller-phone cellular downlink-to-ADB receive path is **not yet runtime-validated on the itel reference device**. Do not describe the synthetic caller as fully automated until that receive path is demonstrated. The next external gate is a disposable no-file downlink probe; only proven behavior should be promoted into supported repository code.
