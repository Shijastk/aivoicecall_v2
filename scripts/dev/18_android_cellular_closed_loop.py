#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from shuo.benchmark.android_cellular_loop import (
    AndroidCellularLoopError,
    DEFAULT_BARGE_DELAY_MS,
    DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    DEFAULT_THINKING_PAUSE_MS,
    MAX_SCENARIO_SECONDS,
    prepare_android_cellular_closed_loop,
    run_android_cellular_closed_loop,
)
from shuo.benchmark.android_cellular_tx import AndroidBuildTools
from shuo.benchmark.android_cellular_rx import AndroidCellularRxError
from shuo.benchmark.android_cellular_tx import AndroidCellularTxError


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_build_dir() -> Path:
    return Path("/tmp/shuo-android-cellular-closed-loop")


async def _run(args) -> int:
    tools = AndroidBuildTools.from_environment()
    missing = tools.missing_host_tools()
    if missing:
        raise AndroidCellularLoopError(
            "missing Android closed-loop host tool(s): " + ", ".join(missing)
        )

    prepared_setup = None
    if args.prepare_before_call:
        prepared_setup = await prepare_android_cellular_closed_loop(
            tools=tools,
            repo_root=_repo_root(),
            build_dir=args.build_dir,
            serial=args.serial,
            response_timeout_seconds=args.response_timeout,
            voice_source=args.voice,
        )
        print("ANDROID_CELLULAR_PRECALL_READY=YES", flush=True)
        print(
            "MANUAL_ACTION=Establish/answer the call, start Terminal 0, "
            "wait for SHUO readiness, then press Enter here.",
            flush=True,
        )
        if not sys.stdin.isatty():
            raise AndroidCellularLoopError(
                "--prepare-before-call requires an interactive terminal"
            )
        await asyncio.to_thread(input)

    report = await run_android_cellular_closed_loop(
        tools=tools,
        repo_root=_repo_root(),
        build_dir=args.build_dir,
        serial=args.serial,
        allow_provider_network=args.allow_provider_network,
        thinking_pause_ms=args.thinking_pause_ms,
        barge_delay_ms=args.barge_delay_ms,
        response_timeout_seconds=args.response_timeout,
        max_scenario_seconds=args.max_scenario_seconds,
        voice_source=args.voice,
        prepared_setup=prepared_setup,
    )

    print(
        "ANDROID_CELLULAR_CLOSED_LOOP="
        + ("PASS" if report.passed else "FAIL")
    )
    for check in report.checks:
        state = (
            "PASS"
            if check.passed is True
            else "FAIL"
            if check.passed is False
            else "NOT_MEASURED"
        )
        print(f"CHECK {check.name}={state}")
    for metric in report.metrics:
        if metric.value is None:
            print(f"METRIC {metric.name}=NOT_MEASURED")
        else:
            print(f"METRIC {metric.name}={metric.value:.3f}{metric.unit}")

    response_metrics = [
        metric
        for metric in report.metrics
        if metric.name.startswith("response_")
        and metric.name.endswith("_tx_end_to_observer_start_ms")
    ]
    for index, metric in enumerate(response_metrics, start=1):
        if metric.value is None:
            value = "NOT_MEASURED"
        else:
            value = f"{metric.value:.3f}ms"
        print(
            f"RESPONSE_LATENCY_{index:02d} "
            f"{metric.name}={value} STATUS={metric.status}"
        )
    print("RESPONSE_LATENCY_KIND=HOST_CORRELATED_OBSERVER")
    print(
        "RESPONSE_LATENCY_SAMPLES="
        f"{report.metadata.get('response_latency_sample_count', 0)}"
    )
    print(
        "RESPONSE_LATENCY_VALID_SAMPLES="
        f"{report.metadata.get('response_latency_valid_sample_count', 0)}"
    )
    print(
        "RESPONSE_STARTED_BEFORE_TX_END="
        f"{report.metadata.get('response_started_before_tx_end_count', 0)}"
    )

    print(
        "RX_TURNS="
        f"{report.metadata.get('rx_end_of_turn_count', 0)}"
    )
    print("RAW_AUDIO_PERSISTED=NO")
    print("TRANSCRIPT_CONTENT_LOGGED=NO")
    print("CALL_CONTROL=MANUAL")
    print("CALLER_HEARD_LATENCY=NOT_MEASURED")

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(report.to_json() + "\n", encoding="utf-8")
        print(f"REPORT={args.json_out}")

    return 0 if report.passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Phase-5 real-cellular deterministic synthetic caller. "
            "Default mode requires an already-active manually controlled call. "
            "The pre-call mode prepares in memory first and then waits for the "
            "operator to establish the call. No raw audio is persisted and no "
            "automatic call control occurs."
        )
    )
    parser.add_argument(
        "--prepare-before-call",
        action="store_true",
        help=(
            "Pre-synthesize caller prompts and compile/push Android bridges "
            "before the call; then wait for manual Enter after the call and "
            "Terminal 0 are ready."
        ),
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
    )
    parser.add_argument(
        "--allow-provider-network",
        action="store_true",
        help="required acknowledgement that the observer contacts Deepgram",
    )
    parser.add_argument(
        "--thinking-pause-ms",
        type=int,
        default=DEFAULT_THINKING_PAUSE_MS,
    )
    parser.add_argument(
        "--barge-delay-ms",
        type=int,
        default=DEFAULT_BARGE_DELAY_MS,
    )
    parser.add_argument(
        "--response-timeout",
        type=float,
        default=DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--max-scenario-seconds",
        type=float,
        default=MAX_SCENARIO_SECONDS,
    )
    parser.add_argument(
        "--voice",
        default=None,
        help="optional Pocket catalog voice alias; default provider voice is used",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help=(
            "optional content-free report path; response transcript text is "
            "never serialized"
        ),
    )

    args = parser.parse_args()

    try:
        return asyncio.run(_run(args))
    except (
        AndroidCellularLoopError,
        AndroidCellularRxError,
        AndroidCellularTxError,
        PermissionError,
        ValueError,
        asyncio.TimeoutError,
    ) as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
