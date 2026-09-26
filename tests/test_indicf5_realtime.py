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
