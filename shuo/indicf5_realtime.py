from __future__ import annotations

from dataclasses import asdict, dataclass
import statistics
import time
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_MODEL_ID = "ai4bharat/IndicF5"
DEFAULT_SAMPLE_RATE = 24_000
DEFAULT_GATE_MS = 500.0


@dataclass(frozen=True)
class IndicF5ProbeSummary:
    device: str
    gpu_name: Optional[str]
    model_load_ms: float
    warmup_ms: float
    runs_ms: tuple[float, ...]
    audio_seconds: tuple[float, ...]
    rtfs: tuple[float, ...]
    median_audio_available_ms: float
    max_audio_available_ms: float
    median_rtf: float
    peak_vram_mb: Optional[float]
    gate_ms: float
    gate_pass: bool

    def to_json_dict(self) -> dict:
        return asdict(self)


def resolve_device(requested: str, *, cuda_available: bool, allow_cpu: bool) -> str:
    normalized = requested.strip().lower()
    if normalized == "auto":
        if cuda_available:
            return "cuda"
        if allow_cpu:
            return "cpu"
        raise RuntimeError(
            "CUDA is not available. This realtime experiment fails closed on CPU "
            "because the reference CPU run was far outside the live-call latency "
            "budget. Use --allow-cpu only for diagnostics."
        )
    if normalized == "cuda":
        if not cuda_available:
            raise RuntimeError("CUDA was requested but torch.cuda.is_available() is false")
        return "cuda"
    if normalized == "cpu":
        if not allow_cpu:
            raise RuntimeError("CPU requires the explicit --allow-cpu diagnostic opt-in")
        return "cpu"
    raise ValueError("device must be one of: auto, cuda, cpu")


def summarize_probe(
    *,
    device: str,
    gpu_name: Optional[str],
    model_load_ms: float,
    warmup_ms: float,
    runs_ms: Iterable[float],
    audio_seconds: Iterable[float],
    peak_vram_mb: Optional[float],
    gate_ms: float = DEFAULT_GATE_MS,
) -> IndicF5ProbeSummary:
    runs = tuple(float(v) for v in runs_ms)
    durations = tuple(float(v) for v in audio_seconds)
    if not runs or len(runs) != len(durations):
        raise ValueError("runs_ms and audio_seconds must be non-empty and the same length")
    if any(v <= 0 for v in runs) or any(v <= 0 for v in durations):
        raise ValueError("timings and audio durations must be positive")
    rtfs = tuple((ms / 1000.0) / seconds for ms, seconds in zip(runs, durations))
    median_ms = statistics.median(runs)
    return IndicF5ProbeSummary(
        device=device,
        gpu_name=gpu_name,
        model_load_ms=float(model_load_ms),
        warmup_ms=float(warmup_ms),
        runs_ms=runs,
        audio_seconds=durations,
        rtfs=rtfs,
        median_audio_available_ms=median_ms,
        max_audio_available_ms=max(runs),
        median_rtf=statistics.median(rtfs),
        peak_vram_mb=peak_vram_mb,
        gate_ms=float(gate_ms),
        gate_pass=median_ms <= gate_ms and max(runs) <= gate_ms,
    )


def run_indicf5_probe(
    *,
    ref_audio: str,
    ref_text: str,
    target_text: str,
    model_id: str = DEFAULT_MODEL_ID,
    device_request: str = "auto",
    allow_cpu: bool = False,
    runs: int = 3,
    gate_ms: float = DEFAULT_GATE_MS,
    warmup_text: str = "ഹലോ.",
    revision: Optional[str] = None,
) -> IndicF5ProbeSummary:
    if runs < 1:
        raise ValueError("runs must be >= 1")
    if gate_ms <= 0:
        raise ValueError("gate_ms must be positive")
    if not ref_text.strip():
        raise ValueError("ref_text is required; do not trigger an ASR side path")
    if not target_text.strip():
        raise ValueError("target_text is required")
    ref_path = Path(ref_audio).expanduser()
    if not ref_path.is_file():
        raise FileNotFoundError(ref_path)

    import numpy as np
    import torch
    from transformers import AutoModel

    device = resolve_device(
        device_request,
        cuda_available=torch.cuda.is_available(),
        allow_cpu=allow_cpu,
    )
    gpu_name = torch.cuda.get_device_name(0) if device == "cuda" else None

    load_kwargs = {"trust_remote_code": True}
    if revision:
        load_kwargs["revision"] = revision

    started = time.perf_counter()
    model = AutoModel.from_pretrained(model_id, **load_kwargs)
    model = model.to(device)
    if hasattr(model, "eval"):
        model.eval()
    if device == "cuda":
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
    model_load_ms = (time.perf_counter() - started) * 1000.0

    def generate(text: str) -> tuple[float, float]:
        if device == "cuda":
            torch.cuda.synchronize()
        started_run = time.perf_counter()
        with torch.inference_mode():
            audio = model(
                text,
                ref_audio_path=str(ref_path),
                ref_text=ref_text,
            )
        if device == "cuda":
            torch.cuda.synchronize()
        elapsed_ms = (time.perf_counter() - started_run) * 1000.0

        if hasattr(audio, "detach"):
            audio = audio.detach().cpu().numpy()
        array = np.asarray(audio).squeeze()
        if array.size == 0:
            raise RuntimeError("IndicF5 returned empty audio")
        duration_s = float(array.size) / DEFAULT_SAMPLE_RATE
        return elapsed_ms, duration_s

    warmup_ms, _ = generate(warmup_text)

    timings = []
    durations = []
    for _ in range(runs):
        elapsed_ms, duration_s = generate(target_text)
        timings.append(elapsed_ms)
        durations.append(duration_s)

    peak_vram_mb = None
    if device == "cuda":
        peak_vram_mb = torch.cuda.max_memory_allocated() / (1024.0 * 1024.0)

    return summarize_probe(
        device=device,
        gpu_name=gpu_name,
        model_load_ms=model_load_ms,
        warmup_ms=warmup_ms,
        runs_ms=timings,
        audio_seconds=durations,
        peak_vram_mb=peak_vram_mb,
        gate_ms=gate_ms,
    )


def find_runtime_modules(wrapper):
    """Find exactly one F5 sampler and one Vocos-style decoder.

    This is intentionally structural rather than tied to private attribute names
    in the gated Hugging Face wrapper. It is used only by the isolated benchmark.
    """
    samplers = []
    decoders = []
    for name, module in wrapper.named_modules():
        if not name:
            continue
        if callable(getattr(module, "sample", None)):
            samplers.append((name, module))
        class_name = module.__class__.__name__.lower()
        if "vocos" in class_name and callable(getattr(module, "decode", None)):
            decoders.append((name, module))

    if len(samplers) != 1:
        names = [name for name, _ in samplers]
        raise RuntimeError(
            f"expected exactly one inner F5 sampler; found {len(samplers)}: {names}"
        )
    if len(decoders) != 1:
        names = [name for name, _ in decoders]
        raise RuntimeError(
            f"expected exactly one Vocos decoder; found {len(decoders)}: {names}"
        )
    return samplers[0], decoders[0]
