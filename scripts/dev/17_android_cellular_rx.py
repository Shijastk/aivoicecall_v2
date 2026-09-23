#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from shuo.benchmark.android_cellular_rx import (
    AdbTelephonyRxBridge,
    AndroidCellularRxError,
    compile_android_rx_bridge,
    preflight_android_cellular_rx,
    probe_android_cellular_rx,
    push_android_rx_bridge,
)
from shuo.benchmark.android_cellular_tx import AndroidBuildTools


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_build_dir() -> Path:
    return Path("/tmp/shuo-android-cellular-rx")


async def _build(args) -> Path:
    tools = AndroidBuildTools.from_environment()
    dex = await compile_android_rx_bridge(
        repo_root=_repo_root(),
        build_dir=args.build_dir,
        tools=tools,
    )
    print(f"DEX={dex}")
    return dex


async def _doctor(args) -> None:
    tools = AndroidBuildTools.from_environment()
    missing = tools.missing_host_tools()
    if missing:
        raise AndroidCellularRxError(
            "missing Android RX host tool(s): " + ", ".join(missing)
        )

    preflight = await preflight_android_cellular_rx(
        tools,
        serial=args.serial,
        require_active_call=not args.allow_no_call,
    )
    print("ADB_DEVICE=READY")
    print(f"ANDROID_SDK={preflight.sdk_int}")
    print(f"APP_PROCESS={preflight.app_process}")
    print(f"ACTUAL_MODE={preflight.actual_mode}")
    print("PRIVAPP_RX_PERMISSIONS=OK")


async def _probe(args) -> None:
    tools = AndroidBuildTools.from_environment()
    missing = tools.missing_host_tools()
    if missing:
        raise AndroidCellularRxError(
            "missing Android RX host tool(s): " + ", ".join(missing)
        )

    preflight = await preflight_android_cellular_rx(
        tools,
        serial=args.serial,
        require_active_call=True,
    )
    dex = await compile_android_rx_bridge(
        repo_root=_repo_root(),
        build_dir=args.build_dir,
        tools=tools,
    )
    await push_android_rx_bridge(
        tools=tools,
        serial=preflight.serial,
        dex_path=dex,
    )

    bridge = AdbTelephonyRxBridge(
        adb=tools.adb,
        serial=preflight.serial,
    )
    try:
        await bridge.start()
        metrics = await probe_android_cellular_rx(
            bridge,
            duration_seconds=args.seconds,
        )
    finally:
        await bridge.close()

    print("ANDROID_TELEPHONY_RX_PROBE=COMPLETE")
    print(f"PCM_BYTES={metrics.pcm_bytes}")
    print(f"PCM_DURATION_SEC={metrics.pcm_duration_seconds:.3f}")
    print(f"CHUNKS={metrics.chunk_count}")
    print(f"PEAK_RMS={metrics.peak_rms}")
    print(f"AVERAGE_RMS={metrics.average_rms:.1f}")
    print(f"SHUO_MULAW_BYTES={metrics.mulaw_bytes}")
    print("RAW_AUDIO_PERSISTED=NO")
    print("CALLER_AUDIO_CONTENT_LOGGED=NO")
    print("CALLER_HEARD_LATENCY=NOT_MEASURED")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Opt-in Phase-5 supplemental Android ADB cellular downlink harness. "
            "It never dials, answers, hangs up, or persists raw audio."
        )
    )
    parser.add_argument(
        "--serial",
        help=(
            "ADB serial. If omitted, exactly one connected state=device target "
            "is required."
        ),
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=_default_build_dir(),
        help="temporary host build directory",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser(
        "doctor",
        help="check host tools, shell capture privileges and current call mode",
    )
    doctor.add_argument(
        "--allow-no-call",
        action="store_true",
        help="diagnostic-only: do not require MODE_IN_CALL",
    )

    sub.add_parser(
        "build",
        help="compile the repository-owned Java receive helper to DEX",
    )

    probe = sub.add_parser(
        "probe",
        help=(
            "consume VOICE_DOWNLINK in memory for a bounded interval and print "
            "content-free byte/energy metrics"
        ),
    )
    probe.add_argument(
        "--seconds",
        type=float,
        default=8.0,
        help="bounded in-memory observation interval; default: 8 seconds",
    )

    args = parser.parse_args()

    try:
        if args.command == "doctor":
            asyncio.run(_doctor(args))
        elif args.command == "build":
            asyncio.run(_build(args))
        elif args.command == "probe":
            asyncio.run(_probe(args))
        else:
            raise AssertionError(args.command)
    except (AndroidCellularRxError, ValueError, asyncio.TimeoutError) as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
