import pytest

from shuo.indicf5_realtime import resolve_device, summarize_probe


def test_realtime_probe_prefers_cuda_and_fails_closed_without_it():
    assert resolve_device("auto", cuda_available=True, allow_cpu=False) == "cuda"
    with pytest.raises(RuntimeError, match="fails closed on CPU"):
        resolve_device("auto", cuda_available=False, allow_cpu=False)
    assert resolve_device("auto", cuda_available=False, allow_cpu=True) == "cpu"


def test_realtime_probe_requires_explicit_cpu_opt_in():
    with pytest.raises(RuntimeError, match="explicit --allow-cpu"):
        resolve_device("cpu", cuda_available=False, allow_cpu=False)


def test_realtime_probe_gate_requires_median_and_max_within_budget():
    passed = summarize_probe(
        device="cuda",
        gpu_name="fake",
        model_load_ms=1000,
        warmup_ms=450,
        runs_ms=[320, 340, 360],
        audio_seconds=[1.0, 1.0, 1.0],
        peak_vram_mb=1234,
        gate_ms=500,
    )
    assert passed.gate_pass
    assert passed.median_audio_available_ms == 340
    assert passed.max_audio_available_ms == 360

    failed = summarize_probe(
        device="cuda",
        gpu_name="fake",
        model_load_ms=1000,
        warmup_ms=450,
        runs_ms=[320, 340, 620],
        audio_seconds=[1.0, 1.0, 1.0],
        peak_vram_mb=1234,
        gate_ms=500,
    )
    assert not failed.gate_pass


def test_realtime_probe_json_is_content_free():
    summary = summarize_probe(
        device="cuda",
        gpu_name="fake",
        model_load_ms=1000,
        warmup_ms=450,
        runs_ms=[300],
        audio_seconds=[1.0],
        peak_vram_mb=1000,
        gate_ms=500,
    )
    payload = summary.to_json_dict()
    assert "ref_audio" not in payload
    assert "ref_text" not in payload
    assert "target_text" not in payload
    assert "generated_audio" not in payload


class _FakeModule:
    def __init__(self, name, *, sample=False, decode=False):
        self.__class__.__name__ = name
        if sample:
            self.sample = lambda *args, **kwargs: None
        if decode:
            self.decode = lambda *args, **kwargs: None


class _FakeWrapper:
    def __init__(self, modules):
        self._modules = modules

    def named_modules(self):
        yield "", self
        for item in self._modules:
            yield item


def test_find_runtime_modules_requires_one_sampler_and_vocos_decoder():
    from shuo.indicf5_realtime import find_runtime_modules

    class Sampler:
        def sample(self):
            pass

    class Vocos:
        def decode(self):
            pass

    sampler = Sampler()
    vocos = Vocos()
    wrapper = _FakeWrapper([("model", sampler), ("vocoder", vocos)])

    (sampler_name, found_sampler), (decoder_name, found_decoder) = find_runtime_modules(wrapper)
    assert sampler_name == "model"
    assert found_sampler is sampler
    assert decoder_name == "vocoder"
    assert found_decoder is vocos


def test_find_runtime_modules_fails_closed_on_ambiguous_sampler():
    from shuo.indicf5_realtime import find_runtime_modules

    class Sampler:
        def sample(self):
            pass

    class Vocos:
        def decode(self):
            pass

    wrapper = _FakeWrapper([
        ("a", Sampler()),
        ("b", Sampler()),
        ("vocoder", Vocos()),
    ])

    with pytest.raises(RuntimeError, match="exactly one logical F5 sampler"):
        find_runtime_modules(wrapper)


def test_find_runtime_modules_collapses_torch_compile_orig_mod_alias():
    from shuo.indicf5_realtime import find_runtime_modules

    class CompiledSampler:
        def sample(self):
            pass

    class OriginalSampler:
        def sample(self):
            pass

    class Vocos:
        def decode(self):
            pass

    outer = CompiledSampler()
    inner = OriginalSampler()
    vocos = Vocos()
    wrapper = _FakeWrapper([
        ("ema_model", outer),
        ("ema_model._orig_mod", inner),
        ("vocoder", vocos),
    ])

    (sampler_name, found_sampler), (decoder_name, found_decoder) = find_runtime_modules(wrapper)
    assert sampler_name == "ema_model"
    assert found_sampler is outer
    assert decoder_name == "vocoder"
    assert found_decoder is vocos


def test_indicf5_mulaw_quality_helpers_round_trip():
    import numpy as np

    from shuo.indicf5_realtime import float_audio_to_mulaw_8k, mulaw_8k_to_pcm16

    samples = np.linspace(-0.5, 0.5, 2400, dtype=np.float32)
    mulaw = float_audio_to_mulaw_8k(samples, 24_000)
    pcm16 = mulaw_8k_to_pcm16(mulaw)

    assert mulaw
    assert pcm16
    assert len(pcm16) == len(mulaw) * 2


def test_indicf5_headroom_helper_scales_only_when_needed():
    import numpy as np

    from shuo.indicf5_realtime import apply_peak_headroom

    quiet = np.array([0.0, 0.25, -0.5], dtype=np.float32)
    adjusted, gain, raw_peak = apply_peak_headroom(quiet, headroom_db=1.0)
    assert raw_peak == pytest.approx(0.5)
    assert gain == pytest.approx(1.0)
    assert np.allclose(adjusted, quiet)

    hot = np.array([0.0, 1.2, -0.8], dtype=np.float32)
    adjusted, gain, raw_peak = apply_peak_headroom(hot, headroom_db=1.0)
    assert raw_peak == pytest.approx(1.2)
    assert gain < 1.0
    assert float(np.max(np.abs(adjusted))) <= 10 ** (-1.0 / 20.0) + 1e-6


def test_remap_compatible_state_dict_handles_orig_mod_aliases():
    import torch

    from shuo.indicf5_realtime import remap_compatible_state_dict

    target = {
        "ema_model._orig_mod.layer.weight": torch.zeros((2, 2), dtype=torch.float32),
        "vocoder._orig_mod.layer.bias": torch.zeros((2,), dtype=torch.float32),
    }
    source = {
        "ema_model.layer.weight": torch.ones((2, 2), dtype=torch.float32),
        "vocoder.layer.bias": torch.ones((2,), dtype=torch.float32),
    }

    remapped = remap_compatible_state_dict(target, source)

    assert set(remapped) == set(target)
    assert torch.equal(
        remapped["ema_model._orig_mod.layer.weight"],
        source["ema_model.layer.weight"],
    )
    assert torch.equal(
        remapped["vocoder._orig_mod.layer.bias"],
        source["vocoder.layer.bias"],
    )


def test_remap_compatible_state_dict_fails_closed_on_shape_mismatch():
    import torch

    from shuo.indicf5_realtime import remap_compatible_state_dict

    target = {"ema_model._orig_mod.layer.weight": torch.zeros((2, 2))}
    source = {"ema_model.layer.weight": torch.zeros((3, 2))}

    with pytest.raises(RuntimeError, match="shape mismatch"):
        remap_compatible_state_dict(target, source)


def test_remap_compatible_state_dict_fails_closed_on_key_mismatch():
    import torch

    from shuo.indicf5_realtime import remap_compatible_state_dict

    target = {"ema_model._orig_mod.layer.weight": torch.zeros((2, 2))}
    source = {"ema_model.other.weight": torch.zeros((2, 2))}

    with pytest.raises(RuntimeError, match="key mismatch"):
        remap_compatible_state_dict(target, source)


def test_indicf5_dev_probe_scripts_compile():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for relative in (
        "scripts/dev/20_indicf5_optimized_probe.py",
        "scripts/dev/22_indicf5_quality_sweep.py",
        "scripts/dev/23_indicf5_checkpoint_validate.py",
    ):
        path = root / relative
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
