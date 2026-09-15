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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run SHUO over an already-active Bluetooth HFP cellular call."
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
    args = parser.parse_args()
    if args.shadow_early_transcripts and not args.shadow_speculation:
        parser.error("--shadow-early-transcripts requires --shadow-speculation")
    if args.shadow_speculation and args.eager_eot_threshold is None:
        parser.error("--shadow-speculation requires --eager-eot-threshold")
    return args


async def _main(args: argparse.Namespace) -> None:
    runner = AsyncioProcessRunner()
    discovery = PwDumpDiscovery(runner)

    session = await build_phase3_ai_only_session(
        discovery=discovery,
        runner=runner,
        bluetooth_address=args.bluetooth_address,
        config=PwCatConfig(latency=args.latency),
    )

    await run_production_bluetooth_conversation(
        session,
        persona_id=args.persona,
        stream_id="bluetooth-manual",
        call_id=args.call_id,
        eager_eot_threshold=args.eager_eot_threshold,
        eot_threshold=args.eot_threshold,
        shadow_speculation=args.shadow_speculation,
        shadow_early_transcripts=args.shadow_early_transcripts,
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
