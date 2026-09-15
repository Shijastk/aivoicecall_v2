"""Opt-in, content-free Bluetooth caller-audio observations. No audio storage."""
from __future__ import annotations

import audioop
import json
import logging
import math
import struct
import time


class AudioEnergy:
    """Constant-space counters; retain at most one split S16LE sample byte.

    Near silence means abs(sample) <= 32 on the signed 16-bit scale.
    Energy is a signal-presence observation, not a speech classifier.
    """

    def __init__(self):
        self.bytes = self.samples = self.squares = self.peak = 0
        self.zero = self.near = 0
        self.tail = b""

    def feed(self, data: bytes, *, mulaw: bool = False):
        self.bytes += len(data)
        pcm = audioop.ulaw2lin(data, 2) if mulaw else self.tail + data
        self.tail = pcm[-1:] if len(pcm) % 2 else b""
        for (sample,) in struct.iter_unpack("<h", pcm[:len(pcm) - len(self.tail)]):
            amplitude = abs(sample)
            self.samples += 1
            self.squares += sample * sample
            self.peak = max(self.peak, amplitude)
            self.zero += amplitude == 0
            self.near += amplitude <= 32

    def snapshot(self):
        return dict(bytes=self.bytes, samples=self.samples,
                    rms=math.sqrt(self.squares / self.samples) if self.samples else 0,
                    peak=self.peak,
                    silence_ratio=self.zero / self.samples if self.samples else None,
                    near_silence_ratio=self.near / self.samples if self.samples else None,
                    near_silence_threshold=32, pending_sample_bytes=len(self.tail))


class BluetoothDiagnostics:
    """One session; one aggregate energy report per second plus final tail.

    Only numerical counters, selected target metadata, and named route results
    leave this object. No transcripts, audio payloads, or provider errors do.
    """

    def __init__(self, *, clock=time.monotonic, emit=None):
        self._clock = clock
        self._emit = emit or self._log
        self._next = clock() + 1.0
        self.raw = AudioEnergy()
        self.mulaw = AudioEnergy()
        self._messages = 0

    @staticmethod
    def _log(record):
        logging.getLogger("shuo.bluetooth.diagnostics").info(
            "BTDiagnostic %s", json.dumps(record, sort_keys=True))

    def record(self, event, **fields):
        # Observations must never interrupt audio delivery or resource cleanup.
        try:
            self._emit(dict(event=event, **fields))
        except Exception:
            pass

    def raw_pcm(self, pcm):
        self.raw.feed(pcm)

    def codec_output(self, mulaw):
        self.mulaw.feed(mulaw, mulaw=True)
        now = self._clock()
        if now >= self._next:
            self.flush()
            self._next = now + 1.0

    def flush(self):
        self.record("audio_energy", raw_pcm=self.raw.snapshot(),
                    codec_mulaw=self.mulaw.snapshot())

    def flux_message(self, message_type, event, transcript_chars):
        # Unknown provider values are never echoed: they may contain content.
        types = {"Connected", "TurnInfo", "FatalError", "Error", "Metadata"}
        events = {"StartOfTurn", "Update", "EndOfTurn", "EagerEndOfTurn", "TurnResumed"}
        self._messages += 1
        self.record("flux_message", message_count=self._messages,
                    message_type=message_type if message_type in types else "unknown",
                    provider_event=event if event in events else "unknown",
                    transcript_chars=transcript_chars)

    def selected(self, direction, target):
        self.record("selected_node", direction=direction, node_name=target.node_name,
                    factory=target.factory_name, media_class=target.media_class,
                    profile=target.bluetooth_profile, codec=target.bluetooth_codec,
                    format=target.audio_format.encoding.value,
                    rate=target.audio_format.sample_rate,
                    channels=target.audio_format.channels)
