from __future__ import annotations

import argparse
import time

from transformers import AutoModel

from shuo.indicf5_realtime import load_compatible_hf_checkpoint

BASE_MODEL_ID = "ai4bharat/IndicF5"
BASE_REVISION = "ba85abedf18dc479a447eaa0eccbd76ab78a47d5"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "CPU-only strict checkpoint compatibility validation. Loads the pinned "
            "base IndicF5 wrapper, remaps torch.compile _orig_mod aliases, then "
            "strict-loads an alternate structurally compatible safetensors checkpoint. "
            "No inference or audio generation is performed."
        )
    )
    parser.add_argument("--checkpoint-repo", required=True)
    parser.add_argument("--checkpoint-file", default="model.safetensors")
    parser.add_argument("--checkpoint-revision", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print("=" * 72)
    print("INDICF5 COMPATIBLE CHECKPOINT STRICT-LOAD VALIDATION")
    print("=" * 72)
    print(f"Base architecture : {BASE_MODEL_ID}@{BASE_REVISION}")
    print(f"Checkpoint repo   : {args.checkpoint_repo}")

    started = time.perf_counter()
    wrapper = AutoModel.from_pretrained(
        BASE_MODEL_ID,
        revision=BASE_REVISION,
        trust_remote_code=True,
    )
    wrapper.eval()
    print(f"Base wrapper load : {(time.perf_counter() - started) * 1000:.1f} ms")

    started = time.perf_counter()
    path = load_compatible_hf_checkpoint(
        wrapper,
        repo_id=args.checkpoint_repo,
        filename=args.checkpoint_file,
        revision=args.checkpoint_revision,
    )
    print(f"Strict load       : {(time.perf_counter() - started) * 1000:.1f} ms")
    print(f"Checkpoint file   : {path}")
    print("RESULT            : STRICT LOAD PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
