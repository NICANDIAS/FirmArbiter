#!/usr/bin/env python3
"""
score_aggregator.py
-------------------
Reads all result JSON files from results/runs/ and produces two outputs:

  1. reports/full_results.csv  — one row per run (every field)
  2. reports/summary.csv       — one row per tool, aggregated metrics

The summary table is what becomes Table X in the paper. Each row shows
a tool's performance across all six benchmark metrics, averaged over
all firmware images in the corpus.

Usage:
    python score_aggregator.py
    python score_aggregator.py --results results/runs/ --out reports/
"""

import argparse
import json
import os
from pathlib import Path

try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
    print("Warning: pandas not installed. Using basic CSV output.")


def load_results(results_dir: str) -> list:
    """
    Load all JSON result files from results_dir.
    Returns a flat list of result dicts.
    """
    results = []
    results_path = Path(results_dir)

    if not results_path.exists():
        print(f"Results directory not found: {results_dir}")
        return results

    json_files = sorted(results_path.glob("*.json"))
    print(f"Loading {len(json_files)} result files from {results_dir}...")

    for fpath in json_files:
        try:
            with open(fpath) as fh:
                result = json.load(fh)
            results.append(result)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  Warning: could not load {fpath.name}: {exc}")

    print(f"Loaded {len(results)} results.")
    return results


def flatten_result(r: dict) -> dict:
    """
    Flatten a nested result dict into a single-level dict suitable
    for a CSV row.
    """
    flat = {
        "run_id":               r.get("run_id"),
        "case_id":              r.get("case_id"),
        "tool":                 r.get("tool"),
        "tool_version":         r.get("tool_version"),
        "run_timestamp_utc":    r.get("run_timestamp_utc"),
        "architecture":         r.get("architecture"),
        "firmware_sha256":      r.get("firmware_sha256", "")[:16],  # short for readability

        # Unpack metrics
        "veritas_rootfs_found":     r.get("unpack", {}).get("veritas_rootfs_found"),
        "veritas_fs_types":         "|".join(r.get("unpack", {}).get("veritas_fs_types", [])),
        "veritas_elf_count":        r.get("unpack", {}).get("veritas_elf_count"),
        "tool_claimed_unpack":      r.get("unpack", {}).get("tool_claimed_unpack"),

        # Boot metrics
        "boot_success":         r.get("boot", {}).get("success"),
        "wall_time_seconds":    r.get("boot", {}).get("wall_time_seconds"),
        "cpu_seconds":          r.get("boot", {}).get("cpu_seconds"),
        "peak_ram_mb":          r.get("boot", {}).get("peak_ram_mb"),
        "timed_out":            r.get("boot", {}).get("timed_out"),
        "failure_reason":       r.get("boot", {}).get("failure_reason"),

        # Service metrics
        "reported_ip":          r.get("service", {}).get("reported_ip"),
        "tcp_connect":          r.get("service", {}).get("tcp_connect"),
        "http_status":          r.get("service", {}).get("http_status"),
        "service_reachable":    r.get("service", {}).get("service_reachable"),
        "service_authentic":    r.get("service", {}).get("service_authentic"),
        "false_positive_reason": r.get("service", {}).get("false_positive_reason"),

        # Stability metrics
        "stability_probe_attempted":    r.get("stability", {}).get("probe_attempted"),
        "service_stable_60s":           r.get("stability", {}).get("service_reachable_at_60s"),
        "service_authentic_at_60s":     r.get("stability", {}).get("service_authentic_at_60s"),

        # Cleanup metrics
        "cleanup_clean":            r.get("cleanup", {}).get("cleanup_clean"),
        "stale_tap_count":          len(r.get("cleanup", {}).get("stale_tap_devices", [])),
        "orphan_qemu_count":        len(r.get("cleanup", {}).get("orphan_qemu_pids", [])),
    }
    return flat


def compute_summary(rows: list) -> list:
    """
    Group rows by tool and compute per-tool summary statistics.
    Returns a list of summary dicts, one per tool.
    """
    from collections import defaultdict

    by_tool = defaultdict(list)
    for row in rows:
        by_tool[row["tool"]].append(row)

    summaries = []
    for tool, tool_rows in sorted(by_tool.items()):
        n = len(tool_rows)

        def pct(field):
            vals = [r[field] for r in tool_rows if r[field] is not None]
            if not vals:
                return None
            return round(100.0 * sum(1 for v in vals if v) / len(vals), 1)

        def avg(field):
            vals = [r[field] for r in tool_rows
                    if r[field] is not None and isinstance(r[field], (int, float))]
            if not vals:
                return None
            return round(sum(vals) / len(vals), 2)

        # False positives: cases where service was reachable but not authentic
        fp_count = sum(
            1 for r in tool_rows
            if r["service_reachable"] and not r["service_authentic"]
        )

        oos_count = sum(
            1 for r in tool_rows
            if r.get("out_of_scope", False)
        )

        # Exclude out-of-scope rows from success/cost calculations
        valid_rows = [r for r in tool_rows if not r.get("out_of_scope", False)]
        n_valid = len(valid_rows)

        def pct_valid(field):
            vals = [r[field] for r in valid_rows if r[field] is not None]
            if not vals:
                return None
            return round(100.0 * sum(1 for v in vals if v) / len(vals), 1)

        def avg_valid(field):
            vals = [r[field] for r in valid_rows
                    if r[field] is not None and isinstance(r[field], (int, float))]
            if not vals:
                return None
            return round(sum(vals) / len(vals), 2)

        summary = {
            "tool":                         tool,
            "total_images":                 n,
            "out_of_scope_encrypted":       oos_count,
            "analysed_images":              n_valid,
            "unpack_success_pct":           pct_valid("veritas_rootfs_found"),
            "boot_success_pct":             pct_valid("boot_success"),
            "service_reachable_pct":        pct_valid("service_reachable"),
            "service_authentic_pct":        pct_valid("service_authentic"),
            "service_stable_60s_pct":       pct_valid("service_stable_60s"),
            "cleanup_clean_pct":            pct_valid("cleanup_clean"),
            "false_positive_count":         fp_count,
            "avg_wall_time_seconds":        avg_valid("wall_time_seconds"),
            "avg_cpu_seconds":              avg_valid("cpu_seconds"),
            "avg_peak_ram_mb":              avg_valid("peak_ram_mb"),
            "timeout_count":                sum(1 for r in valid_rows if r.get("timed_out")),
            "long_running_count":           sum(1 for r in valid_rows
                                                if r.get("wall_time_seconds") and
                                                r["wall_time_seconds"] >= 240),
        }
        summaries.append(summary)

    return summaries


def write_csv_basic(rows: list, out_path: str):
    """Write rows to CSV without pandas."""
    import csv
    if not rows:
        print(f"No rows to write to {out_path}")
        return
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"Written: {out_path}")


def _print_summary_table(summaries: list):
    """
    Print a clean two-block benchmark summary that fits a standard terminal.
    Block 1: success metrics. Block 2: compute cost metrics.
    """
    if not summaries:
        return

    print()
    print("── VERITAS Benchmark Summary ─────────────────────────────────────────")
    print()

    block1 = [
        ("tool",                   "Tool",         12),
        ("total_images",           "Total",         6),
        ("out_of_scope_encrypted", "Encrypted",    10),
        ("analysed_images",        "Analysed",      9),
        ("unpack_success_pct",     "Unpack%",       8),
        ("boot_success_pct",       "Boot%",         7),
        ("service_reachable_pct",  "Reach%",        7),
        ("service_authentic_pct",  "Auth%",         6),
        ("service_stable_60s_pct", "Stable%",       8),
        ("false_positive_count",   "FalsePos",      9),
        ("cleanup_clean_pct",      "Clean%",        7),
    ]

    block2 = [
        ("tool",                   "Tool",         12),
        ("avg_wall_time_seconds",  "AvgWall(s)",   11),
        ("avg_cpu_seconds",        "AvgCPU(s)",    10),
        ("avg_peak_ram_mb",        "PeakRAM(MB)",  12),
        ("timeout_count",          "Timeouts",      9),
        ("long_running_count",     "LongRun(>4m)", 13),
    ]

    def _fmt(val, width):
        if val is None:
            s = "-"
        elif isinstance(val, float):
            s = f"{val:.1f}"
        else:
            s = str(val)
        return s.rjust(width)

    def _print_block(cols):
        header  = "  ".join(label.ljust(w) for _, label, w in cols)
        divider = "  ".join("-" * w for _, _, w in cols)
        print(header)
        print(divider)
        for s in summaries:
            row = "  ".join(_fmt(s.get(key), w) for key, _, w in cols)
            print(row)
        print()

    print("  Success metrics:")
    _print_block(block1)

    print("  Compute cost metrics:")
    _print_block(block2)

    print("  Key:")
    print("    Encrypted    — images skipped (encryption suspected, out of scope)")
    print("    Analysed     — images actually tested (Total minus Encrypted)")
    print("    Unpack%      — images where Binwalk found a root filesystem")
    print("    Boot%        — images where the tool declared emulation success")
    print("    Reach%       — images where a network service responded")
    print("    Auth%        — service was from firmware, not host machine")
    print("    Stable%      — service still up 60s after tool declared success")
    print("    FalsePos     — Reach=True but Auth=False (host page served)")
    print("    Clean%       — tool left no stale TAP devices or QEMU processes")
    print("    AvgWall(s)   — average wall-clock seconds per analysed run")
    print("    AvgCPU(s)    — average CPU seconds consumed per analysed run")
    print("    PeakRAM(MB)  — average peak RAM in MB per analysed run")
    print("    Timeouts     — runs that hit the timeout budget without success")
    print("    LongRun(>4m) — runs that took more than 4 minutes (resource signal)")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="VERITAS score aggregator — produces benchmark comparison tables"
    )
    parser.add_argument(
        "--results", default="results/runs/",
        help="Directory containing result JSON files (default: results/runs/)"
    )
    parser.add_argument(
        "--out", default="reports/",
        help="Output directory for CSV reports (default: reports/)"
    )
    args = parser.parse_args()

    results = load_results(args.results)
    if not results:
        print("No results found. Run run_veritas.py first.")
        return

    rows = [flatten_result(r) for r in results]
    summaries = compute_summary(rows)

    Path(args.out).mkdir(parents=True, exist_ok=True)
    full_path    = os.path.join(args.out, "full_results.csv")
    summary_path = os.path.join(args.out, "summary.csv")

    if PANDAS_AVAILABLE:
        pd.DataFrame(rows).to_csv(full_path, index=False)
        pd.DataFrame(summaries).to_csv(summary_path, index=False)
    else:
        write_csv_basic(rows, full_path)
        write_csv_basic(summaries, summary_path)

    print(f"Written: {full_path}")
    print(f"Written: {summary_path}")
    _print_summary_table(summaries)


if __name__ == "__main__":
    main()
