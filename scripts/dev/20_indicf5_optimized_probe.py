from __future__ import annotations

import argparse
import inspect
import json
import time
from pathlib import Path

import numpy as np
import torch
import torchaudio
from transformers import AutoModel

from f5_tts.infer.utils_infer import infer_batch_process, preprocess_ref_audio_text
from shuo.indicf5_realtime import find_runtime_modules


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Experimental IndicF5 inner-runtime probe. Loads the gated wrapper, "
            "discovers its F5 sampler/Vocos decoder, preprocesses the reference once, "
            "then measures reduced NFE settings. No audio is persisted."
        )
    )
    parser.add_argument("--ref-audio", required=True)
    parser.add_argument("--ref-text", required=True)
    parser.add_argument("--text", default="ഹലോ, സുഖമാണോ?")
    parser.add_argument("--steps", default="16,8,4")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--json-out", default=None)
    return parser.parse_args()


def _normalize_result(result):
    if inspect.isgenerator(result):
        chunks = []
        sr = 24000
        for item in result:
            if not isinstance(item, tuple) or len(item) < 2:
                raise RuntimeError("unexpected streaming result shape")
            chunk, sr = item[0], item[1]
            if chunk is not None and len(chunk):
                chunks.append(np.asarray(chunk))
        if not chunks:
            raise RuntimeError("no generated audio")
        return np.concatenate(chunks), int(sr)

    if not isinstance(result, tuple) or len(result) < 2:
        raise RuntimeError(f"unexpected inference result type: {type(result)!r}")
    audio, sr = result[0], int(result[1])
    if audio is None:
        raise RuntimeError("no generated audio")
    return np.asarray(audio).reshape(-1), sr


def main() -> int:
    args = parse_args()
    steps = tuple(int(x.strip()) for x in args.steps.split(",") if x.strip())
    if not steps or any(step < 1 for step in steps):
        raise SystemExit("--steps must contain positive integers")
    if args.runs < 1:
        raise SystemExit("--runs must be >= 1")

    if not torch.cuda.is_available():
        raise SystemExit("CUDA is required for this optimized realtime experiment")

    device = "cuda"
    print("Loading IndicF5 wrapper...")
    started = time.perf_counter()
    wrapper = AutoModel.from_pretrained(
        "ai4bharat/IndicF5",
        trust_remote_code=True,
    ).to(device)
    wrapper.eval()
    torch.cuda.synchronize()
    print(f"Wrapper load: {(time.perf_counter() - started) * 1000:.1f} ms")

    (sampler_name, sampler), (decoder_name, decoder) = find_runtime_modules(wrapper)
    print(f"Sampler: {sampler_name} ({sampler.__class__.__name__})")
    print(f"Decoder: {decoder_name} ({decoder.__class__.__name__})")

    print("Preprocessing reference once...")
    started = time.perf_counter()
    processed_ref, processed_text = preprocess_ref_audio_text(
        args.ref_audio,
        args.ref_text,
        show_info=lambda *_: None,
        device=device,
    )
    audio, sr = torchaudio.load(processed_ref)
    preprocess_ms = (time.perf_counter() - started) * 1000.0
    ref_seconds = audio.shape[-1] / sr
    print(f"Reference duration after preprocessing: {ref_seconds:.3f} s")
    print(f"Reference preprocessing: {preprocess_ms:.1f} ms")

    results = []
    for step in steps:
        for run_index in range(1, args.runs + 1):
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()

            started = time.perf_counter()
            result = infer_batch_process(
                (audio, sr),
                processed_text,
                [args.text],
                sampler,
                decoder,
                progress=None,
                nfe_step=step,
                device=device,
            )
            generated, out_sr = _normalize_result(result)
            torch.cuda.synchronize()
            elapsed_ms = (time.perf_counter() - started) * 1000.0

            audio_seconds = generated.size / out_sr
            rtf = (elapsed_ms / 1000.0) / audio_seconds
            peak_mb = torch.cuda.max_memory_allocated() / (1024.0 * 1024.0)

            row = {
                "nfe_step": step,
                "run": run_index,
                "elapsed_ms": elapsed_ms,
                "audio_seconds": audio_seconds,
                "rtf": rtf,
                "peak_vram_mb": peak_mb,
            }
            results.append(row)
            print(
                f"NFE={step:<2} run={run_index}: {elapsed_ms:.1f} ms  "
                f"audio={audio_seconds:.3f}s  rtf={rtf:.3f}  "
                f"peak={peak_mb:.1f} MiB"
            )

    payload = {
        "device": device,
        "gpu": torch.cuda.get_device_name(0),
        "reference_seconds": ref_seconds,
        "reference_preprocess_ms": preprocess_ms,
        "results": results,
    }

    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"content-free JSON: {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
