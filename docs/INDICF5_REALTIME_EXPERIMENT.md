# IndicF5 realtime cloned-voice experiment

**Status:** experimental benchmark only. Not a production TTS provider and not a
Phase-5 acceptance claim.

## Why this exists

The owner locally verified that AI4Bharat IndicF5 can synthesize new Malayalam
sentences with an owner-supplied reference recording while preserving the speaker
characteristics well enough to sound like the owner. The same reference host's
CPU-only experiment was not realtime: a 6.79-second output required about
165.56 seconds of generation (RTF about 24.36).

That quality observation is useful, but it does not authorize routing live calls
through IndicF5. The next question is narrower: can the already
reference-conditioned voice produce the first bounded phrase quickly enough on
the reference GPU to justify building a SHUO provider?

## Important model behavior

IndicF5's documented Hugging Face interface takes three inputs on every synthesis:

- target text;
- reference prompt audio; and
- the exact reference transcript.

This is reference-conditioned / zero-shot voice cloning, not a one-time trained
speaker checkpoint.

The current IndicF5 AutoModel wrapper returns a complete generated waveform for
the submitted text. Upstream F5-TTS exposes a socket/chunk mode, but its current
implementation first synthesizes a text batch and then yields waveform chunks.
Therefore this experiment does not label complete-phrase generation time as true
streaming TTFA.

## Safety and isolation

The experiment:

- is opt-in and lives in shuo/indicf5_realtime.py plus scripts/dev;
- does not change TTS_PROVIDER, TTSPool, Agent, the state machine, carriers or
  Bluetooth production wiring;
- does not persist generated audio;
- does not log or write the reference transcript, reference path or target text
  to the JSON result;
- keeps the reference recording outside the repository;
- fails closed on CPU unless --allow-cpu is explicitly supplied; and
- makes no caller mouth-to-ear claim.

## Reference-host environment

The owner's working local IndicF5 environment used Python 3.12 with the gated
ai4bharat/IndicF5 model, Transformers 4.50.0 and Hugging Face Hub 0.29.3.
Transformers 5.x produced a model/vocoder device initialization failure in the
observed setup. TorchCodec was also required by the installed TorchAudio
torchaudio.load path.

For the realtime experiment, use an isolated environment or the already-working
local IndicF5 environment. Do not add the IndicF5/CUDA stack to the repository's
default dependencies.

Example reference-host setup, after verifying enough free disk space:

~~~bash
python3.12 -m venv ~/venvs/indicf5-gpu
source ~/venvs/indicf5/bin/activate
python -m pip install --upgrade pip setuptools wheel

pip install \
  torch==2.11.0 \
  torchaudio==2.11.0 \
  torchcodec==0.11.1 \
  --index-url https://download.pytorch.org/whl/cu126

pip install "numpy<2" "transformers==4.50.0" "huggingface_hub==0.29.3"
pip install git+https://github.com/ai4bharat/IndicF5.git
~~~

The exact CUDA wheel remains a reference-host experiment, not a portable project
dependency.

## Probe

The reference audio and transcript stay local. Example:

~~~bash
source ~/venvs/indicf5-gpu/bin/activate

python scripts/dev/19_indicf5_realtime_probe.py \
  --ref-audio /home/shijas/Downloads/myvoice.m4a \
  --ref-text '<exact transcript of the reference recording>' \
  --text 'ഹലോ, സുഖമാണോ?' \
  --runs 3 \
  --gate-ms 500 \
  --json-out /tmp/shuo/indicf5-realtime.json
~~~

The default target phrase is deliberately short, close to the existing local TTS
bounded-phrase behavior. A pass requires both median and maximum measured wrapper
audio-available latency to be at or below the requested gate.

The script also reports generated-audio RTF and CUDA peak allocated memory. An
OOM, missing CUDA device or latency gate failure is a failed experiment, not a
reason to weaken the threshold.

## Decision gate before provider integration

Do not add TTS_PROVIDER=indicf5 until all of the following are true on the
reference host:

1. the model and vocoder fit the GTX 1650 Max-Q 4 GB without OOM;
2. repeated warm first-phrase audio availability is acceptably low;
3. no new dependency is forced into the default ElevenLabs/Pocket install;
4. cloned-voice quality remains acceptable for several unseen Malayalam phrases;
5. an 8 kHz G.711 mu-law conversion A/B remains intelligible; and
6. a provider design can preserve bounded queues, cancellation and the existing
   no-whole-answer-buffering contract.

Even a 500 ms component-local result is not a SHUO caller-heard under-500 ms
result. STT EOT, LLM TTFT, player pre-roll and transport remain separate latency
terms.

If this benchmark passes, the next change on this same experimental branch is an
opt-in IndicF5TTSService behind the existing provider router, followed by focused
tests and manual speaker/cellular validation before any production merge.


## Reference GTX 1650 CUDA result — 2026-09-26

The owner ran the isolated probe on the reference MSI laptop with
NVIDIA GeForce GTX 1650 Max-Q. PyTorch reported CUDA available and the model
loaded successfully; this was not an OOM failure.

Observed content-free metrics for the short target phrase:

- model load: 6898.9 ms
- warm-up: 42579.9 ms
- run 1: 51995.6 ms for 0.747 s generated audio (RTF 69.606)
- run 2: 85086.1 ms for 0.747 s generated audio (RTF 113.904)
- run 3: 156786.9 ms for 0.747 s generated audio (RTF 209.889)
- median audio-available latency: 85086.1 ms
- maximum audio-available latency: 156786.9 ms
- peak PyTorch allocated VRAM: 1419.3 MiB
- experimental 500 ms gate: **FAIL**

This proves only that the current IndicF5 Hugging Face AutoModel inference path is
not realtime on this reference GPU. It does not prove that every possible
optimized IndicF5/F5-TTS runtime is too slow. Do not add TTS_PROVIDER=indicf5
from this path.

The increasing per-run time is observed evidence only; thermal throttling, hidden
CPU work, dtype choice or another cause must not be asserted without profiling.
The next experiment, if pursued, must isolate model sampling, reference
preprocessing and vocoder cost and may test lower NFE values without weakening
the production latency target.


## Reduced-NFE profiling follow-up

After the default AutoModel gate failed, the branch adds
`scripts/dev/20_indicf5_optimized_probe.py`. This remains benchmark-only.

The probe discovers the wrapper's inner F5 sampler and Vocos decoder structurally,
preprocesses the reference once, and calls the installed AI4Bharat inference
utility directly with explicit NFE values. This tests whether the default
32-step flow is the dominant cost without changing the production gate.

Start conservatively with one run at NFE 8 and 4:

~~~bash
PYTHONPATH=. python scripts/dev/20_indicf5_optimized_probe.py \
  --ref-audio /home/shijas/Downloads/myvoice.m4a \
  --ref-text '<exact reference transcript>' \
  --text 'ഹലോ, സുഖമാണോ?' \
  --steps 8,4 \
  --runs 1 \
  --json-out /tmp/shuo/indicf5-optimized.json
~~~

Reduced NFE is an experiment, not an automatic optimization win. Latency and
voice/pronunciation quality must both be checked. If the long reference remains
far from realtime, repeat only after recording a clean short reference clip with
an exact transcript; do not silently truncate and guess the transcript.


### Probe discovery note

The gated wrapper on the reference CUDA host exposed the compiled sampler twice
through named_modules(): ema_model and ema_model._orig_mod. The probe now
treats that specific torch.compile wrapper/original pair as one logical sampler
while continuing to fail closed if two genuinely independent samplers or
decoders are discovered.


## Reduced-NFE reference result — 2026-09-26

The owner ran the inner-runtime probe on the same GTX 1650 Max-Q CUDA host with
the same mobile reference recording. The probe discovered the compiled sampler
and Vocos decoder, preprocessed the reference once, and measured reduced diffusion
steps without routing through SHUO production.

Observed content-free evidence:

- wrapper load: 3036.6 ms
- preprocessed reference duration: 12.924 s
- one-time reference preprocessing: 233.7 ms
- NFE 8: 10581.2 ms for 0.747 s generated audio, RTF 14.171, peak allocated VRAM 1408.1 MiB
- NFE 4: 5279.6 ms for 0.747 s generated audio, RTF 7.071, peak allocated VRAM 1405.6 MiB

This materially improves on the default 32-step AutoModel path but remains more
than an order of magnitude above the 500 ms experimental component gate. The
evidence also shows that reference preprocessing itself is not the dominant cost
in this run; it took 233.7 ms while the NFE-4 synthesis took 5279.6 ms.

Do not infer an exact lower bound for shorter references or NFE values that were
not measured. A short-reference / lower-NFE probe may still be used to close the
local feasibility question, but no IndicF5 provider integration is authorized by
these results.


## L40S reduced-NFE result — 2026-09-27

The owner ran the same inner-runtime IndicF5 probe on a Lightning AI NVIDIA L40S
host (PyTorch 2.8.0+cu128, TorchAudio 2.8.0+cu128, TorchCodec 0.7.0).

Observed content-free metrics with the same 12.924-second preprocessed reference
and the same short target phrase:

- wrapper load: 18604.9 ms
- one-time reference preprocessing: 180.0 ms
- NFE 8: 1583.4 ms for 0.747 s generated audio, RTF 2.121, peak allocated VRAM 1407.1 MiB
- NFE 4: 284.7 ms for 0.747 s generated audio, RTF 0.381, peak allocated VRAM 1404.6 MiB

The NFE-4 run passes the experimental 500 ms component-local audio-available
gate. This is a materially different result from the GTX 1650 reference host.
It is not yet a production acceptance result: the probe returns the completed
bounded phrase, so 284.7 ms is not a true streaming first-sample TTFA and not
caller mouth-to-ear latency. Voice quality at NFE 4, repeated-run stability,
8 kHz mu-law quality and cancellation behavior still require validation before
provider integration.


## L40S NFE-4 stability run — 2026-09-27

The owner repeated the same short-phrase L40S probe for five NFE-4 runs.

Observed content-free metrics:

- wrapper load: 3340.4 ms
- reference preprocessing: 170.6 ms
- run 1: 663.5 ms, RTF 0.889
- run 2: 282.4 ms, RTF 0.378
- run 3: 280.6 ms, RTF 0.376
- run 4: 276.0 ms, RTF 0.370
- run 5: 275.0 ms, RTF 0.368
- generated audio duration: 0.747 s
- peak allocated VRAM: 1404.6 MiB

Interpretation: after the first post-load/compile run, the four steady runs were
275.0–282.4 ms. The first run exceeded the 500 ms component target, so a
production design would need an explicit startup warm-up before accepting live
traffic. This remains complete bounded-phrase audio-available latency, not true
first-sample TTFA or caller mouth-to-ear latency. Voice quality at NFE 4 and
8 kHz mu-law quality still require manual validation before provider integration.


## Manual NFE-4 voice-quality gate

After latency and stability pass on L40S, use
`scripts/dev/21_indicf5_quality_probe.py` before provider integration.

The manual probe deliberately writes only generated/synthetic audio under
`/tmp/indicf5-quality` by default; it does not record caller audio and it does
not write the reference recording into the repository. It pins the same IndicF5
revision used by the measured experiments, performs one startup warm-up, then
writes:

- `indicf5-nfe4-native.wav`: native generated PCM for voice/pronunciation review;
- `indicf5-nfe4-8k.ulaw`: exact 8 kHz G.711 mu-law payload; and
- `indicf5-nfe4-telephony.wav`: the same mu-law payload decoded back to PCM16 so
  the owner can listen to the approximate caller-side codec quality.

Run the focused CPU tests before spending GPU credits:

~~~bash
python -m pytest tests/test_indicf5_realtime.py -q
~~~

Then switch to L40S only for the manual quality generation:

~~~bash
PYTHONPATH=. python scripts/dev/21_indicf5_quality_probe.py \
  --ref-audio /teamspace/studios/this_studio/myvoice.m4a \
  --ref-text '<exact reference transcript>' \
  --nfe-step 4
~~~

Manual acceptance requires both native and telephony WAVs to preserve recognizable
speaker identity, understandable Malayalam pronunciation and acceptable pacing
without obvious artifacts. This quality gate does not change the production
provider router.


## L40S NFE-4 manual-quality generation result — 2026-09-27

The owner ran `scripts/dev/21_indicf5_quality_probe.py` on the L40S after an
explicit warm-up. The probe generated the native cloned-voice WAV and the exact
8 kHz G.711 mu-law telephone simulation artifacts.

Observed content-free runtime metrics:

- wrapper load: 3244.4 ms
- reference duration after preprocessing: 12.924 s
- NFE: 4
- generation time: 396.4 ms
- generated audio duration: 6.165 s
- RTF: 0.064
- peak allocated VRAM: 1424.5 MiB

This passes the experimental component-local 500 ms complete-phrase generation
gate for this longer utterance. Manual listening acceptance is still pending:
speaker identity, Malayalam pronunciation, pacing, artifacts and 8 kHz mu-law
intelligibility must be reviewed before provider integration.

The focused pytest suite was not executed in that Lightning session because
pytest was not installed in the Studio environment; do not treat this manual
generation as an automated-test pass.


## Manual quality review and focused tests — 2026-09-27

The owner completed the focused CPU test suite after the L40S quality generation:

- tests/test_indicf5_realtime.py: 8 passed
- one expected Python 3.12 audioop deprecation warning

Manual listening feedback on the generated NFE-4 artifacts was not accepted as
production-quality yet. The owner reported:

- an audible room/echo-like character, especially near the beginning;
- some Malayalam articulation/pronunciation that did not sound fully natural;
- otherwise recognizable voice identity in portions of the sample; and
- a desire for higher overall voice quality before integration.

Inspection of the uploaded generated WAVs showed that the room/echo-like
character is already present in the native 24 kHz artifact, so the 8 kHz G.711
mu-law conversion is not the sole source of that artifact. The native PCM16 WAV
also reaches digital full scale, so the next quality comparison should preserve
headroom before encoding.

Do not integrate the provider from the NFE-4 quality sample alone. The next
bounded experiment is an A/B/C quality sweep at NFE 4, 6 and 8 with identical
reference/text and controlled output headroom. A clean reference recording should
be reviewed separately if the room character persists at higher NFE.


## Owner A/B feedback and next hardware/language gate — 2026-09-27

The owner manually compared the generated sweep. NFE 4 native and telephony
outputs were rejected as poor quality. NFE 6 and NFE 8 were materially more
acceptable, although not yet final-production quality. A mixed-English phrase
("voice assistant") was also pronounced unnaturally.

This is consistent with an important model-boundary fact: IndicF5 officially
lists 11 Indian languages and does not list English. Mixed English should
therefore not be treated as a supported-quality guarantee.

The previous NFE 4/6/8 sweep did not reset a common random seed before each NFE
generation, so it was not a perfectly controlled NFE-only listening comparison.
The sweep script now accepts --seed and resets the same RNG state before each NFE
value.

The next bounded experiment is:
1. run a seed-controlled NFE 5/6/8 quality/latency sweep on a Lightning L4;
2. compare against the L40S evidence without changing the production latency
   threshold;
3. separately A/B raw mixed-English text against Malayalam-script phonetic
   normalization for common English loanwords.

L4 is chosen only as a cost/performance experiment; no latency claim is made
before measurement.


## L4 and Indian-English/code-mix candidate gate — 2026-09-27

Current owner listening decision:
- NFE 4 native and telephony outputs are rejected for quality.
- NFE 6 and NFE 8 are materially more acceptable, but final production quality
  is not yet accepted.
- mixed-English pronunciation remains a visible gap with base IndicF5.

Base IndicF5 officially lists 11 Indian languages and does not list English.
Therefore mixed-English quality is not treated as guaranteed behavior.

A current compatible candidate, dheeyantra/dhee-indic-f5, is a third-party
IndicF5 fine-tune whose model card explicitly targets Indian-accented English,
code-mixed input, the same Indian-language family including Malayalam, and
zero-shot reference-audio voice cloning. It advertises the same IndicF5
inference interface and approximately the same 0.4B model scale. Its license is
CC-BY-NC-4.0, so it is experimental/non-commercial unless separate licensing is
obtained. No production-quality claim is accepted from the model card alone.

The quality sweep now resets a common RNG seed before each NFE value so future
A/B comparisons are NFE-controlled rather than also changing the random sample.

The next hardware experiment is Lightning L4, not L40S. Current Lightning
pricing lists L4 at $0.48/GPU-hour versus L40S at $2.14/GPU-hour. Memory fit is
not expected to be the limiting factor because measured IndicF5 peak PyTorch
allocation on L40S was about 1.4 GiB, but L4 latency must be measured rather
than inferred.

No provider integration is authorized until the L4 latency and code-mix
listening gates are complete.


## L4 latency and Dhee packaging result — 2026-09-27

The owner ran the base IndicF5 latency probe on a Lightning NVIDIA L4 (23034 MiB).

Observed short-phrase metrics:
- NFE 5: 2077.6 ms first run, then 972.4 ms and 962.6 ms
- NFE 6: 1161.9 ms, 1170.1 ms, 1171.8 ms
- NFE 8: 1582.8 ms, 1546.5 ms, 1540.1 ms
- peak PyTorch allocation stayed about 1405-1407 MiB

Therefore L4 is materially cheaper but does not meet the 500 ms component-local
target at the NFE values that remain quality candidates. Do not choose L4 for
the current realtime target from this evidence.

A base IndicF5 mixed-English quality sweep also completed on L4:
- NFE 6: 1352.5 ms for 2.741 s audio, RTF 0.493
- NFE 8: 1779.5 ms for 2.741 s audio, RTF 0.649

The attempted dheeyantra/dhee-indic-f5 AutoModel load failed before inference.
Transformers reported an unrecognized model because the repository does not
currently expose the config/custom-code metadata expected by AutoModel. This is
a packaging/loading failure, not a voice-quality or latency result. The Dhee
model card claims the same IndicF5 architecture and Indian-English/code-mix
support, so any further Dhee experiment must first verify the actual safetensors
key layout and load those weights through a known-good IndicF5 architecture
rather than assuming AutoModel works from the repo id.


## Dhee checkpoint structural comparison — 2026-09-27

CPU-only safetensors inspection compared the pinned base IndicF5 checkpoint with
dheeyantra/dhee-indic-f5.

Raw key comparison showed 447 tensors in each checkpoint but no direct key
intersection because the pinned base checkpoint stores compiled-module aliases
under `ema_model._orig_mod.*` and `vocoder._orig_mod.*`, while Dhee stores
the corresponding tensors under `ema_model.*` and `vocoder.*`.

After normalizing the `ema_model._orig_mod.` prefix, 364 model tensors matched
with zero observed shape or dtype differences. The remaining 83 missing/extra
keys were exactly the Vocos vocoder group, where the same
`vocoder._orig_mod.` versus `vocoder.` naming difference remained.

This is strong evidence of checkpoint-layout compatibility but is not yet an
exact compatibility proof. The next CPU-only check must normalize `._orig_mod`
for both sampler and vocoder prefixes and compare all 447 key names and shapes
before any custom checkpoint loader or paid-GPU inference is attempted.


## Dhee exact normalized compatibility and strict loader — 2026-09-27

A second CPU-only checkpoint comparison normalized every occurrence of
`._orig_mod.` in both the F5 sampler and Vocos vocoder key paths.

Observed result:

- base tensors: 447
- Dhee tensors: 447
- normalized common tensors: 447
- missing: 0
- extra: 0
- shape differences: 0
- dtype differences: 0
- result: **exact structural compatibility after compile-alias normalization**

This is sufficient to build a fail-closed experimental checkpoint loader without
using Dhee's currently incomplete AutoModel packaging. The branch now includes:

- `remap_compatible_state_dict()`, which requires one-to-one normalized key
  coverage plus identical shapes and dtypes;
- `load_compatible_hf_checkpoint()`, which downloads and strict-loads the
  compatible safetensors checkpoint into the pinned base IndicF5 wrapper; and
- `scripts/dev/23_indicf5_checkpoint_validate.py`, a CPU-only strict-load
  validation that performs no inference or audio generation.

Unit tests cover compile-alias remapping plus fail-closed shape/key mismatch
behavior. These new tests must pass before paid-GPU Dhee inference is attempted.


## Dhee strict-load validation pass — 2026-09-27

The owner completed the fail-closed validation path on the Lightning CPU host:

- focused tests: 12 passed, 1 expected audioop deprecation warning;
- normalized checkpoint structure: 447/447 tensors matched;
- missing/extra tensors: 0/0;
- shape differences: 0;
- dtype differences: 0;
- pinned base IndicF5 wrapper load: PASS;
- Dhee compatible checkpoint strict load: PASS.

The experimental latency and quality probes now support loading a compatible
checkpoint through the pinned base IndicF5 architecture with
`--checkpoint-repo`. Direct `AutoModel.from_pretrained` against the Dhee
repository remains unsupported because that repository does not currently ship
the custom AutoModel packaging metadata used by the base IndicF5 repository.

Before the next paid-GPU run, perform CPU syntax compilation and rerun the
focused tests. The next paid run should use L40S, not L4, because the measured
L4 NFE 6/8 latencies miss the 500 ms component target.
