#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from shuo.benchmark.conversation import (
    BenchmarkReport,
    analyze_bluetooth_log,
    compare_reports,
    run_offline_benchmark,
    run_provider_benchmark,
)

from shuo.benchmark.flux_pipeline import (
    DEFAULT_FIXTURE_PATH,
    DEFAULT_MANIFEST_PATH,
    prepare_flux_fixture,
    run_flux_pipeline_benchmark,
)
from shuo.benchmark.human_sim import (
    DEFAULT_THINKING_PAUSE_MS,
    MAX_SCENARIO_SECONDS,
    run_human_sim_benchmark,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _print_report(report: BenchmarkReport) -> None:
    print(f"mode: {report.mode}")
    commit = report.metadata.get("git_commit")
    if commit:
        print(f"git:  {commit}{' (dirty)' if report.metadata.get('git_dirty') else ''}")

    print("\nchecks")
    for item in report.checks:
        state = "PASS" if item.passed is True else "FAIL" if item.passed is False else item.status
        suffix = f" — {item.note}" if item.note else ""
        print(f"  {state:<14} {item.name}{suffix}")

    print("\nmetrics")
    for metric in report.metrics:
        if metric.median_ms is None:
            print(f"  {metric.name:<44} {metric.status}")
        else:
            print(
                f"  {metric.name:<44} n={metric.count:<3} "
                f"median={metric.median_ms:.3f}ms p95={metric.p95_ms:.3f}ms "
                f"[{metric.status}]"
            )

    if report.raw.get("comparisons"):
        print("\ncomparisons")
        for row in report.raw["comparisons"]:
            print(
                f"  {row['name']:<44} "
                f"{row['before_median_ms']:.3f} -> {row['after_median_ms']:.3f}ms "
                f"delta={row['delta_median_ms']:+.3f}ms"
            )

    print("\nlimitations")
    for item in report.limitations:
        print(f"  - {item}")


def _write_json(report: BenchmarkReport, path: Path | None) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_json() + "\n", encoding="utf-8")
    print(f"\njson: {path}")


def _load_report(path: Path) -> BenchmarkReport:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema") != "shuo.conversation-benchmark/v1":
        raise ValueError(f"Unsupported report schema in {path}")

    from shuo.benchmark.conversation import CheckResult, Metric

    metrics = []
    for item in raw.get("metrics", []):
        metrics.append(
            Metric(
                name=item["name"],
                samples_ms=tuple(item.get("samples_ms") or ()),
                status=item.get("status", "NOT_MEASURED"),
                scope=item.get("scope", ""),
                start_boundary=item.get("start_boundary", ""),
                end_boundary=item.get("end_boundary", ""),
                clock=item.get("clock", ""),
                note=item.get("note", ""),
            )
        )
    checks = [
        CheckResult(
            name=item["name"],
            passed=item.get("passed"),
            status=item.get("status", "UNKNOWN"),
            note=item.get("note", ""),
        )
        for item in raw.get("checks", [])
    ]
    return BenchmarkReport(
        mode=raw.get("mode", "unknown"),
        metrics=metrics,
        checks=checks,
        metadata=raw.get("metadata", {}),
        limitations=raw.get("limitations", []),
        raw=raw.get("raw", {}),
    )


async def _run(args) -> BenchmarkReport:
    root = _repo_root()
    if args.command == "offline":
        return await run_offline_benchmark(repo_root=root)
    if args.command == "providers":
        return await run_provider_benchmark(
            allow_provider_network=args.allow_provider_network,
            repo_root=root,
            timeout_seconds=args.timeout,
        )
    if args.command == "flux-pipeline":
        return await run_flux_pipeline_benchmark(
            audio_path=args.fixture,
            manifest_path=args.manifest,
            allow_provider_network=args.allow_provider_network,
            repo_root=root,
            runs=args.runs,
            timeout_seconds=args.timeout,
            eot_threshold=args.eot_threshold,
            history_turns=args.history_turns,
        )
    if args.command == "human-sim":
        return await run_human_sim_benchmark(
            allow_provider_network=args.allow_provider_network,
            repo_root=root,
            timeout_seconds=args.timeout,
            thinking_pause_ms=args.thinking_pause_ms,
            max_scenario_seconds=args.max_scenario_seconds,
        )
    raise AssertionError(args.command)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evidence-labelled SHUO conversation benchmark. Default mode is provider/device-free."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    offline = sub.add_parser("offline", help="real orchestration/Agent/Player with injected fake providers")
    offline.add_argument("--json-out", type=Path)

    providers = sub.add_parser(
        "providers",
        help="real Groq+ElevenLabs, injected Flux events, no Deepgram/Bluetooth/cellular",
    )
    providers.add_argument(
        "--allow-provider-network",
        action="store_true",
        help="required acknowledgement that this mode spends provider requests/credits",
    )
    providers.add_argument("--timeout", type=float, default=45.0)
    providers.add_argument("--json-out", type=Path)

    prepare = sub.add_parser(
        "prepare-flux-fixture",
        help="generate and freeze one synthetic S16LE16k caller fixture using current ElevenLabs TTS",
    )
    prepare.add_argument(
        "--allow-provider-network",
        action="store_true",
        help="required acknowledgement that fixture generation spends an ElevenLabs request",
    )
    prepare.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE_PATH)
    prepare.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    prepare.add_argument("--overwrite", action="store_true")
    prepare.add_argument("--timeout", type=float, default=45.0)

    flux_pipeline = sub.add_parser(
        "flux-pipeline",
        help="frozen synthetic audio -> real Deepgram Flux -> real Groq -> real ElevenLabs; no device/cellular",
    )
    flux_pipeline.add_argument(
        "--allow-provider-network",
        action="store_true",
        help="required acknowledgement that this mode spends Deepgram/Groq/ElevenLabs requests",
    )
    flux_pipeline.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE_PATH)
    flux_pipeline.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    flux_pipeline.add_argument("--runs", type=int, default=5)
    flux_pipeline.add_argument("--timeout", type=float, default=45.0)
    flux_pipeline.add_argument(
        "--eot-threshold",
        type=float,
        default=None,
        help=(
            "Deepgram Flux final EndOfTurn confidence threshold "
            "(0.5-1.0). Omit to preserve provider default 0.7."
        ),
    )
    flux_pipeline.add_argument(
        "--history-turns",
        type=int,
        default=0,
        help=(
            "Seed N deterministic completed prior user/assistant turns before "
            "the measured turn. Benchmark-only; valid range 0-64."
        ),
    )
    flux_pipeline.add_argument("--json-out", type=Path)

    human_sim = sub.add_parser(
        "human-sim",
        help=(
            "multi-turn Pocket synthetic caller -> real Deepgram Flux -> real Groq -> "
            "Pocket agent, with thinking pause + two barge-ins + continuity; "
            "no PipeWire/Bluetooth/cellular"
        ),
    )
    human_sim.add_argument(
        "--allow-provider-network",
        action="store_true",
        help="required acknowledgement that this mode contacts Deepgram and Groq",
    )
    human_sim.add_argument("--timeout", type=float, default=45.0)
    human_sim.add_argument(
        "--thinking-pause-ms",
        type=int,
        default=DEFAULT_THINKING_PAUSE_MS,
        help="silence inserted inside one synthetic caller question",
    )
    human_sim.add_argument(
        "--max-scenario-seconds",
        type=float,
        default=MAX_SCENARIO_SECONDS,
        help="hard Phase-5 scenario cap; values above 300 are rejected",
    )
    human_sim.add_argument("--json-out", type=Path)

    log = sub.add_parser("log", help="analyze an existing content-free Bluetooth lifecycle log")
    log.add_argument("path", type=Path)
    log.add_argument("--json-out", type=Path)

    compare = sub.add_parser("compare", help="compare two JSON reports only where provenance matches")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.add_argument("--json-out", type=Path)

    args = parser.parse_args()

    try:
        if args.command == "prepare-flux-fixture":
            manifest = asyncio.run(
                prepare_flux_fixture(
                    audio_path=args.fixture,
                    manifest_path=args.manifest,
                    allow_provider_network=args.allow_provider_network,
                    overwrite=args.overwrite,
                    timeout_seconds=args.timeout,
                )
            )
            print("fixture prepared")
            print(f"  audio:    {args.fixture}")
            print(f"  manifest: {args.manifest}")
            print(f"  sha256:   {manifest.sha256}")
            print(f"  bytes:    {manifest.bytes}")
            print(f"  duration: {manifest.duration_ms:.3f}ms")
            return 0
        if args.command == "log":
            report = analyze_bluetooth_log(
                args.path.read_text(encoding="utf-8", errors="replace"),
                source_name=str(args.path),
            )
        elif args.command == "compare":
            report = compare_reports(_load_report(args.before), _load_report(args.after))
        else:
            report = asyncio.run(_run(args))
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    _print_report(report)
    _write_json(report, getattr(args, "json_out", None))

    # Only explicit failed checks fail the command. NOT_MEASURED/UNKNOWN is not
    # rewritten as PASS or FAIL.
    return 1 if any(item.passed is False for item in report.checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
