#!/usr/bin/env python3
"""Explicit manual Bluetooth-AI runner.

This script does NOT answer, originate, or hang up cellular calls.
Start/answer the cellular call manually first, then run this command while the
HFP call endpoints exist. Ctrl+C stops only the owned AI/PipeWire session.
"""

from __future__ import annotations

import argparse
import asyncio

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
        help=(
            "Phase-4A measurement only. Enable Deepgram EagerEndOfTurn at an "
            "explicit 0.3-0.7 threshold; AI still waits for final EndOfTurn."
        ),
    )
    return parser.parse_args()


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
    )


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
