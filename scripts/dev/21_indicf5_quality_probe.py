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
    find_runtime_modules,
    float_audio_to_mulaw_8k,
    mulaw_8k_to_pcm16,
)


DEFAULT_REVISION = "ba85abedf18dc479a447eaa0eccbd76ab78a47d5"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Manual IndicF5 cloned-voice quality probe. Generates one native WAV "
            "and one G.711 mu-law telephone-simulated WAV under /tmp by default. "
            "This does not modify the production TTS provider router."
        )
    )
    parser.add_argument("--ref-audio", required=True)
    parser.add_argument("--ref-text", required=True)
    parser.add_argument(
        "--text",
        default=(
            "ഹലോ, ഞാൻ ഇപ്പോൾ ഒരു പുതിയ വോയ്സ് സിസ്റ്റം പരീക്ഷിക്കുകയാണ്. "
            "നിങ്ങൾക്ക് ഇന്ന് എന്താണ് സഹായം വേണ്ടത്?"
        ),
    )
    parser.add_argument("--nfe-step", type=int, default=4)
    parser.add_argument("--out-dir", default="/tmp/indicf5-quality")
    parser.add_argument("--revision", default=DEFAULT_REVISION)
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
    if args.nfe_step < 1:
        raise SystemExit("--nfe-step must be >= 1")
    if not torch.cuda.is_available():
        raise SystemExit("CUDA is required for this manual quality probe")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Loading pinned IndicF5 wrapper...")
    started = time.perf_counter()
    wrapper = AutoModel.from_pretrained(
        "ai4bharat/IndicF5",
        revision=args.revision,
        trust_remote_code=True,
    ).to("cuda")
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

    # Explicit startup warm-up so the measured/manual sample is steady-state.
    print("Warm-up...")
    with torch.inference_mode():
        warm = infer_batch_process(
            (ref_audio, ref_sr),
            processed_text,
            ["ഹലോ."],
            sampler,
            decoder,
            progress=None,
            nfe_step=args.nfe_step,
            device="cuda",
        )
        normalize_result(warm)
    torch.cuda.synchronize()

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
            nfe_step=args.nfe_step,
            device="cuda",
        )
        generated, out_sr = normalize_result(result)
    torch.cuda.synchronize()
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    native_path = out_dir / "indicf5-nfe4-native.wav"
    sf.write(native_path, generated.astype(np.float32), out_sr, subtype="PCM_16")

    mulaw = float_audio_to_mulaw_8k(generated, out_sr)
    raw_mulaw_path = out_dir / "indicf5-nfe4-8k.ulaw"
    raw_mulaw_path.write_bytes(mulaw)

    telephony_path = out_dir / "indicf5-nfe4-telephony.wav"
    write_pcm16_wav(telephony_path, mulaw_8k_to_pcm16(mulaw), 8_000)

    audio_seconds = generated.size / out_sr
    rtf = (elapsed_ms / 1000.0) / audio_seconds
    peak_mb = torch.cuda.max_memory_allocated() / (1024.0 * 1024.0)

    print("=" * 72)
    print("INDICF5 MANUAL QUALITY RESULT")
    print("=" * 72)
    print(f"NFE                       : {args.nfe_step}")
    print(f"generation                : {elapsed_ms:.1f} ms")
    print(f"generated audio           : {audio_seconds:.3f} s")
    print(f"RTF                       : {rtf:.3f}")
    print(f"peak allocated VRAM       : {peak_mb:.1f} MiB")
    print(f"native WAV                : {native_path}")
    print(f"telephone WAV             : {telephony_path}")
    print(f"raw G.711 mu-law          : {raw_mulaw_path}")
    print()
    print("Listen to native and telephone WAVs and compare voice identity,")
    print("Malayalam pronunciation, pacing, artifacts and 8 kHz intelligibility.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
