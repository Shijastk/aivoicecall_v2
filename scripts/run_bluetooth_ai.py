#!/usr/bin/env python3
"""Explicit manual Bluetooth-AI runner.

This script does NOT answer, originate, or hang up cellular calls.
Start/answer the cellular call manually first, then run this command while the
HFP call endpoints exist. Ctrl+C stops only the owned AI/PipeWire session.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from shuo.bluetooth.phase3_session import build_phase3_ai_only_session
from shuo.bluetooth.pipewire_live import PwCatConfig, PwDumpDiscovery
from shuo.bluetooth.process import AsyncioProcessRunner
from shuo.bluetooth.production import run_production_bluetooth_conversation


def _eager_threshold(value: str) -> float:
    try:
        threshold = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc

    # Phase 4A deliberately leaves Flux's final EOT threshold at its provider
    # default (0.7), so eager must not exceed it. Wider tuning belongs to a
    # separately measured plan update.
    if not 0.3 <= threshold <= 0.7:
        raise argparse.ArgumentTypeError("must be between 0.3 and 0.7")
    return threshold


def _eot_threshold(value: str) -> float:
    try:
        threshold = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc

    if not 0.5 <= threshold <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0.5 and 1.0")
    return threshold


def _phase4d_phrase_chars(value: str) -> int:
    try:
        chars = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 24 <= chars <= 160:
        raise argparse.ArgumentTypeError("must be between 24 and 160")
    return chars


def _positive_chars(value: str) -> int:
    try:
        chars = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if chars <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return chars


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run SHUO over an already-active Bluetooth HFP cellular call."
    )
    parser.add_argument(
        "--diagnose-caller-audio", action="store_true",
        help="Content-free Bluetooth PCM/codec/Flux/route diagnostics; no audio recording.",
    )
    parser.add_argument(
        "--bluetooth-address",
        required=True,
        help="Explicit phone Bluetooth address, e.g. AA:BB:CC:DD:EE:FF",
    )
    parser.add_argument(
        "--latency",
        required=True,
        help="Explicit pw-cat latency, e.g. 40ms. No production default is assumed.",
    )
    parser.add_argument(
        "--persona",
        default="default",
        help="Existing SHUO persona/config id (default: default).",
    )
    parser.add_argument(
        "--call-id",
        default="bluetooth-manual",
        help="Local trace id only; this is not a carrier call id.",
    )
    parser.add_argument(
        "--eager-eot-threshold",
        type=_eager_threshold,
        default=None,
        help="Enable Deepgram EagerEndOfTurn at an explicit 0.3-0.7 threshold.",
    )
    parser.add_argument(
    "--eot-threshold",
    type=_eot_threshold,
    default=0.5,
        help=(
            "Optional Deepgram Flux final EndOfTurn threshold (0.5-1.0). "
            "Omit to preserve provider default."
        ),
    )
    parser.add_argument(
        "--shadow-speculation",
        action="store_true",
        help=(
            "Phase-4B test mode: start an early first-token probe. "
            "Generated text is discarded and never reaches TTS/history."
        ),
    )
    parser.add_argument(
        "--shadow-early-transcripts", action="store_true",
        help="Opt in to bounded repeated-Update shadow probes before eager EOT.",
    )
    parser.add_argument(
        "--prepared-response-reuse",
        action="store_true",
        help=(
            "Phase-4C opt-in: reuse a matching first-token-ready speculative "
            "LLM stream after final EndOfTurn; normal generation is fallback."
        ),
    )
    parser.add_argument(
        "--tts-phrase-chars",
        type=_phase4d_phrase_chars,
        default=None,
        help=(
            "Phase-4D opt-in: batch LLM text into punctuation/size-bounded TTS "
            "phrases; explicit 24-160 character cap required."
        ),
    )
    parser.add_argument(
        "--llm-history-max-chars",
        type=_positive_chars,
        default=None,
        help=(
            "Phase-4D opt-in: bound provider-visible conversation history by "
            "characters while retaining canonical in-memory history and the full system prompt."
        ),
    )
    parser.add_argument(
        "--llm-provider-timing",
        action="store_true",
        help="Phase-4D opt-in: request/log content-free streaming usage timing when supported.",
    )
    parser.add_argument(
        "--parallel-startup",
        action="store_true",
        help="Phase-4D opt-in: warm Flux and TTS/Agent concurrently with fail-clean teardown.",
    )
    parser.add_argument(
        "--player-preroll-frames",
        type=int,
        choices=(2, 3),
        default=3,
        help="Phase-4D controlled A/B knob; rules.md C5 permits only 2 or 3 frames.",
    )
    args = parser.parse_args()
    if args.shadow_early_transcripts and not args.shadow_speculation:
        parser.error("--shadow-early-transcripts requires --shadow-speculation")
    if args.prepared_response_reuse and not args.shadow_speculation:
        parser.error("--prepared-response-reuse requires --shadow-speculation")
    if args.shadow_speculation and args.eager_eot_threshold is None:
        parser.error("--shadow-speculation requires --eager-eot-threshold")
    return args


async def _main(args: argparse.Namespace) -> None:
    diagnostics = None
    if args.diagnose_caller_audio:
        from shuo.bluetooth.diagnostics import BluetoothDiagnostics
        diagnostics = BluetoothDiagnostics()
    runner = AsyncioProcessRunner()
    discovery = PwDumpDiscovery(runner)

    session = await build_phase3_ai_only_session(
        discovery=discovery,
        runner=runner,
        bluetooth_address=args.bluetooth_address,
        config=PwCatConfig(latency=args.latency),
        diagnostics=diagnostics,
    )

    await run_production_bluetooth_conversation(
        session,
        persona_id=args.persona,
        diagnostics=diagnostics,
        stream_id="bluetooth-manual",
        call_id=args.call_id,
        eager_eot_threshold=args.eager_eot_threshold,
        eot_threshold=args.eot_threshold,
        shadow_speculation=args.shadow_speculation,
        shadow_early_transcripts=args.shadow_early_transcripts,
        prepared_response_reuse=args.prepared_response_reuse,
        tts_phrase_chars=args.tts_phrase_chars,
        llm_history_max_chars=args.llm_history_max_chars,
        llm_provider_timing=args.llm_provider_timing,
        parallel_startup=args.parallel_startup,
        player_preroll_frames=args.player_preroll_frames,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    args = parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
