# Product vision

**PLANNED — task-owner product direction, not current integration.**

The goal is a general SHUO voice-call system preserving Vobiz/Twilio virtual-
number functionality and browser/V2 behavior while adding an isolated Bluetooth
cellular-call transport.

```text
Compatible Android cellular phone (HFP Audio Gateway)
  → Bluetooth call downlink on supported Linux
  → direct digital capture → boundary conversion → SHUO STT
  → existing low-latency LLM/agent pipeline → streaming TTS
  → boundary conversion → direct digital Bluetooth uplink → cellular caller
```

No laptop speaker-to-microphone acoustic bridge should be needed. Separate
paths must prevent AI speech entering STT. The product needs controlled call
lifecycle handling, safe disconnect/device-loss/stream-failure cleanup,
observable operation, reproducible tests and a reversible rollout.

Compatibility is capability-based and earned through the
[matrix](COMPATIBILITY.md). Neither universal Android nor universal Linux
Bluetooth/PipeWire compatibility is promised. The reference session establishes
one audio contract, not an integrated SHUO release. Call control must be validated
independently of audio. Existing carrier/browser changes need their own approval.
The [roadmap](ROADMAP.md) separates software, devices, provider integration,
real calls, lifecycle completion, resilience and release gates.
