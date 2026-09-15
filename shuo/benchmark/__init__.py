"""Opt-in development benchmarks for SHUO.

Nothing in this package is imported by the production entrypoints.
"""

from .conversation import (
    BenchmarkReport,
    CheckResult,
    Metric,
    analyze_bluetooth_log,
    compare_reports,
    run_offline_benchmark,
    run_provider_benchmark,
)

__all__ = [
    "BenchmarkReport",
    "CheckResult",
    "Metric",
    "analyze_bluetooth_log",
    "compare_reports",
    "run_offline_benchmark",
    "run_provider_benchmark",
]
