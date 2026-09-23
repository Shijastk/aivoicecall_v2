#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from shuo.benchmark.android_cellular_tx import (
    AdbTelephonyTxBridge,
    AndroidBuildTools,
    AndroidCellularTxError,
    compile_android_tx_bridge,
    preflight_android_cellular_tx,
    push_android_tx_bridge,
    stream_pocket_utterance_to_bridge,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_build_dir() -> Path:
    return Path("/tmp/shuo-android-cellular-tx")


async def _build(args) -> Path:
    tools = AndroidBuildTools.from_environment()
    dex = await compile_android_tx_bridge(
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
        raise AndroidCellularTxError(
            "missing Android TX host tool(s): " + ", ".join(missing)
        )
    preflight = await preflight_android_cellular_tx(
        tools,
        serial=args.serial,
        require_active_call=not args.allow_no_call,
    )
    print(f"ADB_SERIAL={preflight.serial}")
    print(f"ANDROID_SDK={preflight.sdk_int}")
    print(f"APP_PROCESS={preflight.app_process}")
    print(f"ACTUAL_MODE={preflight.actual_mode}")
    print("PRIVAPP_ROUTE_PERMISSIONS=OK")


async def _speak(args) -> None:
    tools = AndroidBuildTools.from_environment()
    missing = tools.missing_host_tools()
    if missing:
        raise AndroidCellularTxError(
            "missing Android TX host tool(s): " + ", ".join(missing)
        )

    preflight = await preflight_android_cellular_tx(
        tools,
        serial=args.serial,
        require_active_call=True,
    )
    dex = await compile_android_tx_bridge(
        repo_root=_repo_root(),
        build_dir=args.build_dir,
        tools=tools,
    )
    await push_android_tx_bridge(
        tools=tools,
        serial=preflight.serial,
        dex_path=dex,
    )

    bridge = AdbTelephonyTxBridge(
        adb=tools.adb,
        serial=preflight.serial,
    )
    try:
        await bridge.start()
        metrics = await stream_pocket_utterance_to_bridge(
            args.text,
            bridge,
            voice_source=args.voice,
            timeout_seconds=args.timeout,
        )
        exit_code = await bridge.close()
    except BaseException:
        await bridge.abort()
        raise

    print("ANDROID_TELEPHONY_TX=PASS")
    print(f"PCM_BYTES={metrics.pcm_bytes}")
    print(f"PCM_DURATION_SEC={metrics.pcm_duration_seconds:.3f}")
    print(f"MODEL_READY_MS_LOCAL={metrics.model_ready_ms:.1f}")
    if metrics.first_pcm_ms is not None:
        print(f"FIRST_PCM_MS_LOCAL={metrics.first_pcm_ms:.1f}")
    if metrics.model_ready_to_first_pcm_ms is not None:
        print(
            "MODEL_READY_TO_FIRST_PCM_MS_LOCAL="
            f"{metrics.model_ready_to_first_pcm_ms:.1f}"
        )
    print(f"ADB_EXIT={exit_code}")
    print("CALLER_HEARD_LATENCY=NOT_MEASURED")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Opt-in Phase-5 supplemental Android ADB synthetic-caller TX harness. "
            "It never dials, answers, hangs up, or records raw audio."
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
        help=(
            "temporary host build directory; default: "
            "/tmp/shuo-android-cellular-tx"
        ),
    )

    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser(
        "doctor",
        help="check host tools, ADB shell privileges and current call mode",
    )
    doctor.add_argument(
        "--allow-no-call",
        action="store_true",
        help="diagnostic-only: do not require MODE_IN_CALL",
    )

    sub.add_parser(
        "build",
        help="compile the repository-owned Java helper to DEX",
    )

    speak = sub.add_parser(
        "speak",
        help=(
            "stream one Pocket utterance into an already-active manual "
            "cellular call"
        ),
    )
    speak.add_argument(
        "text",
        help="synthetic caller text; it is not logged by this harness",
    )
    speak.add_argument(
        "--voice",
        default=None,
        help="optional Pocket catalog voice alias; default is provider-owned alba",
    )
    speak.add_argument("--timeout", type=float, default=45.0)

    args = parser.parse_args()

    try:
        if args.command == "doctor":
            asyncio.run(_doctor(args))
        elif args.command == "build":
            asyncio.run(_build(args))
        elif args.command == "speak":
            asyncio.run(_speak(args))
        else:
            raise AssertionError(args.command)
    except (AndroidCellularTxError, ValueError, asyncio.TimeoutError) as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
