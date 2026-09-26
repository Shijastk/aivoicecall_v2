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
