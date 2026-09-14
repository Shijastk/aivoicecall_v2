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
    )


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
