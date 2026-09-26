from __future__ import annotations

import argparse
import json
from pathlib import Path

from shuo.benchmark.indicf5_realtime import run_indicf5_probe


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Content-free IndicF5 warm phrase-latency probe. This does not modify "
            "the production TTS router and does not persist generated audio."
        )
    )
    parser.add_argument("--ref-audio", required=True)
    parser.add_argument("--ref-text", required=True)
    parser.add_argument(
        "--text",
        default="ഹലോ, സുഖമാണോ?",
        help="Short target phrase close to the production phrase cap.",
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--gate-ms", type=float, default=500.0)
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument(
        "--allow-cpu",
        action="store_true",
        help="Diagnostic only; realtime qualification fails closed on CPU by default.",
    )
    parser.add_argument("--revision", default=None)
    parser.add_argument("--json-out", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = run_indicf5_probe(
        ref_audio=args.ref_audio,
        ref_text=args.ref_text,
        target_text=args.text,
        device_request=args.device,
        allow_cpu=args.allow_cpu,
        runs=args.runs,
        gate_ms=args.gate_ms,
        revision=args.revision,
    )

    print("=" * 72)
    print("INDICF5 REALTIME PHRASE PROBE")
    print("=" * 72)
    print(f"device                    : {summary.device}")
    print(f"gpu                       : {summary.gpu_name or 'n/a'}")
    print(f"model load                : {summary.model_load_ms:.1f} ms")
    print(f"warm-up                   : {summary.warmup_ms:.1f} ms")
    for index, (ms, seconds, rtf) in enumerate(
        zip(summary.runs_ms, summary.audio_seconds, summary.rtfs),
        start=1,
    ):
        print(
            f"run {index:<2}                   : {ms:.1f} ms  "
            f"audio={seconds:.3f}s  rtf={rtf:.3f}"
        )
    print(f"median audio available    : {summary.median_audio_available_ms:.1f} ms")
    print(f"max audio available       : {summary.max_audio_available_ms:.1f} ms")
    print(f"median RTF                : {summary.median_rtf:.3f}")
    if summary.peak_vram_mb is not None:
        print(f"peak allocated VRAM       : {summary.peak_vram_mb:.1f} MiB")
    print(f"experimental gate         : {'PASS' if summary.gate_pass else 'FAIL'}")
    print()
    print(
        "Important: IndicF5's current AutoModel wrapper returns a complete phrase "
        "waveform. This metric is therefore audio-available latency for the first "
        "bounded phrase, not true streaming sample TTFA and not caller mouth-to-ear."
    )

    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(summary.to_json_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"content-free JSON: {path}")

    return 0 if summary.gate_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
