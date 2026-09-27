from __future__ import annotations

import argparse
import inspect
import time
import wave
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio
from transformers import AutoModel

from f5_tts.infer.utils_infer import infer_batch_process, preprocess_ref_audio_text
from shuo.indicf5_realtime import (
    apply_peak_headroom,
    find_runtime_modules,
    float_audio_to_mulaw_8k,
    mulaw_8k_to_pcm16,
)

DEFAULT_REVISION = "ba85abedf18dc479a447eaa0eccbd76ab78a47d5"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "IndicF5 manual quality sweep. Loads the model once, performs one "
            "startup warm-up, then generates matched native and 8 kHz mu-law "
            "samples for multiple NFE values."
        )
    )
    parser.add_argument("--ref-audio", required=True)
    parser.add_argument("--model-id", default="ai4bharat/IndicF5")
    parser.add_argument("--revision", default=None)
    parser.add_argument("--checkpoint-repo", default=None)
    parser.add_argument("--checkpoint-file", default="model.safetensors")
    parser.add_argument("--checkpoint-revision", default=None)
    parser.add_argument("--ref-text", required=True)
    parser.add_argument(
        "--text",
        default=(
            "ഹലോ, ഞാൻ ഇപ്പോൾ ഒരു പുതിയ വോയ്സ് സിസ്റ്റം പരീക്ഷിക്കുകയാണ്. "
            "നിങ്ങൾക്ക് ഇന്ന് എന്താണ് സഹായം വേണ്ടത്?"
        ),
    )
    parser.add_argument("--steps", default="4,6,8")
    parser.add_argument("--headroom-db", type=float, default=1.0)
    parser.add_argument("--out-dir", default="/tmp/indicf5-quality-sweep")
    parser.add_argument("--seed", type=int, default=1234)
    return parser.parse_args()


def normalize_result(result):
    if inspect.isgenerator(result):
        chunks = []
        sr = 24_000
        for item in result:
            if not isinstance(item, tuple) or len(item) < 2:
                raise RuntimeError("unexpected streaming result shape")
            chunk, sr = item[0], item[1]
            if chunk is not None and len(chunk):
                chunks.append(np.asarray(chunk))
        if not chunks:
            raise RuntimeError("no generated audio")
        return np.concatenate(chunks).reshape(-1), int(sr)

    if not isinstance(result, tuple) or len(result) < 2:
        raise RuntimeError(f"unexpected inference result type: {type(result)!r}")
    audio, sr = result[0], int(result[1])
    if audio is None:
        raise RuntimeError("no generated audio")
    return np.asarray(audio).reshape(-1), sr


def write_pcm16_wav(path: Path, pcm16: bytes, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm16)


def main() -> int:
    args = parse_args()
    steps = tuple(int(x.strip()) for x in args.steps.split(",") if x.strip())
    if not steps or any(step < 1 for step in steps):
        raise SystemExit("--steps must contain positive integers")
    if args.headroom_db < 0:
        raise SystemExit("--headroom-db must be >= 0")
    if not torch.cuda.is_available():
        raise SystemExit("CUDA is required for this manual quality sweep")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Loading pinned IndicF5 wrapper once...")
    started = time.perf_counter()
    revision = args.revision
    model_id = args.model_id
    if args.checkpoint_repo:
        if model_id != "ai4bharat/IndicF5":
            raise SystemExit(
                "--checkpoint-repo requires the pinned ai4bharat/IndicF5 base architecture"
            )
        revision = revision or DEFAULT_REVISION
    elif revision is None and model_id == "ai4bharat/IndicF5":
        revision = DEFAULT_REVISION

    load_kwargs = {"trust_remote_code": True}
    if revision:
        load_kwargs["revision"] = revision
    wrapper = AutoModel.from_pretrained(
        model_id,
        **load_kwargs,
    )
    if args.checkpoint_repo:
        checkpoint_path = load_compatible_hf_checkpoint(
            wrapper,
            repo_id=args.checkpoint_repo,
            filename=args.checkpoint_file,
            revision=args.checkpoint_revision,
        )
        print(f"Checkpoint loaded          : {args.checkpoint_repo}")
        print(f"Checkpoint file            : {checkpoint_path}")
    wrapper = wrapper.to("cuda")
    wrapper.eval()
    torch.cuda.synchronize()
    print(f"Wrapper load: {(time.perf_counter() - started) * 1000:.1f} ms")

    (sampler_name, sampler), (decoder_name, decoder) = find_runtime_modules(wrapper)
    print(f"Sampler: {sampler_name} ({sampler.__class__.__name__})")
    print(f"Decoder: {decoder_name} ({decoder.__class__.__name__})")

    processed_ref, processed_text = preprocess_ref_audio_text(
        args.ref_audio,
        args.ref_text,
        show_info=lambda *_: None,
        device="cuda",
    )
    ref_audio, ref_sr = torchaudio.load(processed_ref)
    print(f"Reference duration: {ref_audio.shape[-1] / ref_sr:.3f} s")

    print("Startup warm-up...")
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    np.random.seed(args.seed)
    with torch.inference_mode():
        warm = infer_batch_process(
            (ref_audio, ref_sr),
            processed_text,
            ["ഹലോ."],
            sampler,
            decoder,
            progress=None,
            nfe_step=min(steps),
            device="cuda",
        )
        normalize_result(warm)
    torch.cuda.synchronize()

    print("=" * 88)
    print("INDICF5 QUALITY SWEEP")
    print("=" * 88)
    print(f"Base model                : {model_id}")
    print(f"Checkpoint repo           : {args.checkpoint_repo or \"(base weights)\"}")

    for step in steps:
        # Reset the same RNG state for each NFE value so the A/B/C comparison
        # changes diffusion-step count without also changing the random sample.
        torch.manual_seed(args.seed)
        torch.cuda.manual_seed_all(args.seed)
        np.random.seed(args.seed)
        torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        with torch.inference_mode():
            result = infer_batch_process(
                (ref_audio, ref_sr),
                processed_text,
                [args.text],
                sampler,
                decoder,
                progress=None,
                nfe_step=step,
                device="cuda",
            )
            generated, out_sr = normalize_result(result)
        torch.cuda.synchronize()
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        generated, gain, raw_peak = apply_peak_headroom(
            generated,
            headroom_db=args.headroom_db,
        )

        native_path = out_dir / f"indicf5-nfe{step}-native.wav"
        sf.write(native_path, generated.astype(np.float32), out_sr, subtype="PCM_16")

        mulaw = float_audio_to_mulaw_8k(generated, out_sr)
        raw_mulaw_path = out_dir / f"indicf5-nfe{step}-8k.ulaw"
        raw_mulaw_path.write_bytes(mulaw)

        telephony_path = out_dir / f"indicf5-nfe{step}-telephony.wav"
        write_pcm16_wav(telephony_path, mulaw_8k_to_pcm16(mulaw), 8_000)

        audio_seconds = generated.size / out_sr
        rtf = (elapsed_ms / 1000.0) / audio_seconds
        peak_mb = torch.cuda.max_memory_allocated() / (1024.0 * 1024.0)

        print(
            f"NFE={step:<2} generation={elapsed_ms:7.1f} ms  "
            f"audio={audio_seconds:5.3f}s  rtf={rtf:5.3f}  "
            f"raw_peak={raw_peak:5.3f}  gain={gain:5.3f}  "
            f"peak_vram={peak_mb:7.1f} MiB"
        )
        print(f"  native    : {native_path}")
        print(f"  telephony : {telephony_path}")

    print()
    print(f"Seed                      : {args.seed}")
    print("A/B/C the native files first. Then compare matching telephony files.")
    print("Prefer the lowest NFE that preserves voice identity, Malayalam clarity,")
    print("natural pacing and absence of room/echo artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
