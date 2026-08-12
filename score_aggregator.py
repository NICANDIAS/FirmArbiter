#!/usr/bin/env python3
"""
Aggregate Adapter Contract v1 FIRMARBITER results.

The aggregator reads immutable ``final-result.json`` files, selects one
experiment, resolves repeated attempts deterministically, and writes CSV
reports without mixing historical experiments.

Examples:
    ./python score_aggregator.py --list-experiments
    ./python score_aggregator.py --experiment-id pilot-01
    ./python score_aggregator.py --experiment-id pilot-01 --attempt 2
    ./python score_aggregator.py

When no experiment is supplied, the most recently completed experiment is
selected and reported explicitly.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_RESULTS_ROOT = PROJECT_ROOT / "results" / "runs"
DEFAULT_REPORTS_ROOT = PROJECT_ROOT / "reports"


class AggregationError(RuntimeError):
    """Raised when results cannot be selected safely."""


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def safe_token(value: str) -> str:
    token = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        value,
    ).strip("._-")
    return token[:128] or "experiment"


def nested(document: Any, *path: str) -> Any:
    current = document

    for component in path:
        if not isinstance(current, dict):
            return None
        current = current.get(component)

    return current


def as_bool_status(value: Any) -> bool | None:
    if value == "true" or value is True:
        return True
    if value == "false" or value is False:
        return False
    return None


def first_numeric(
    document: Any,
    keys: Iterable[str],
) -> float | None:
    wanted = set(keys)

    def walk(value: Any) -> float | None:
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    key in wanted
                    and isinstance(child, (int, float))
                    and not isinstance(child, bool)
                ):
                    return float(child)

            for child in value.values():
                result = walk(child)
                if result is not None:
                    return result

        elif isinstance(value, list):
            for child in value:
                result = walk(child)
                if result is not None:
                    return result

        return None

    return walk(document)


def list_status(records: Any) -> str:
    if not isinstance(records, list) or not records:
        return "not_reported"

    statuses = [
        nested(
            record,
            "independent_measurement",
            "status",
        )
        for record in records
        if isinstance(record, dict)
    ]

    if "true" in statuses:
        return "true"
    if "false" in statuses:
        return "false"
    if "indeterminate" in statuses:
        return "indeterminate"
    if "probe_error" in statuses:
        return "probe_error"

    available = [
        str(value)
        for value in statuses
        if value is not None
    ]
    return available[0] if available else "not_reported"


def endpoint_claim_count(result: dict[str, Any]) -> int:
    claims = result.get("candidate_claims", [])
    return sum(
        1
        for claim in claims
        if isinstance(claim, dict)
        and claim.get("event") == "endpoint_reported"
    )


def candidate_claim_seen(
    result: dict[str, Any],
    event_name: str,
) -> bool:
    claims = result.get("candidate_claims", [])
    return any(
        isinstance(claim, dict)
        and claim.get("event") == event_name
        for claim in claims
    )


def candidate_stage_record(
    result: dict[str, Any],
    stage: str,
) -> dict[str, Any]:
    records = result.get("candidate_stage_results", [])

    if not isinstance(records, list):
        return {}

    for record in records:
        if (
            isinstance(record, dict)
            and record.get("stage") == stage
        ):
            return record

    return {}


def load_neutral_results(
    results_root: Path,
) -> list[dict[str, Any]]:
    if not results_root.is_dir():
        raise AggregationError(
            f"Results root does not exist: {results_root}"
        )

    result_paths = sorted(
        path
        for path in results_root.rglob("final-result.json")
        if "_experiments" not in path.parts
    )

    results: list[dict[str, Any]] = []

    for path in result_paths:
        try:
            document = json.loads(
                path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"Warning: could not load {path}: {exc}",
                file=sys.stderr,
            )
            continue

        run = document.get("run")

        if (
            not isinstance(run, dict)
            or not run.get("experiment_id")
            or not run.get("case_id")
            or not run.get("adapter_id")
        ):
            print(
                f"Warning: ignoring non-v1 result: {path}",
                file=sys.stderr,
            )
            continue

        document["_source_path"] = str(path)
        results.append(document)

    if not results:
        raise AggregationError(
            f"No Adapter Contract v1 final-result.json files found "
            f"under {results_root}"
        )

    return results


def result_timestamp(result: dict[str, Any]) -> str:
    return str(
        result.get("completed_at")
        or result.get("created_at")
        or ""
    )


def experiment_inventory(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for result in results:
        grouped[result["run"]["experiment_id"]].append(result)

    inventory = []

    for experiment_id, experiment_results in grouped.items():
        latest = max(
            (result_timestamp(item) for item in experiment_results),
            default="",
        )
        cases = {
            item["run"]["case_id"]
            for item in experiment_results
        }
        adapters = {
            item["run"]["adapter_id"]
            for item in experiment_results
        }

        inventory.append(
            {
                "experiment_id": experiment_id,
                "result_files": len(experiment_results),
                "cases": len(cases),
                "adapters": sorted(adapters),
                "latest_timestamp": latest,
            }
        )

    return sorted(
        inventory,
        key=lambda item: (
            item["latest_timestamp"],
            item["experiment_id"],
        ),
        reverse=True,
    )


def choose_experiment(
    results: list[dict[str, Any]],
    requested: str | None,
) -> str:
    inventory = experiment_inventory(results)
    available = {
        item["experiment_id"]
        for item in inventory
    }

    if requested:
        if requested not in available:
            raise AggregationError(
                f"Experiment {requested!r} was not found. Available: "
                + ", ".join(sorted(available))
            )
        return requested

    selected = inventory[0]["experiment_id"]
    print(
        "No --experiment-id supplied; selecting the most recent "
        f"experiment: {selected}"
    )
    return selected


def select_attempts(
    results: list[dict[str, Any]],
    *,
    experiment_id: str,
    attempt: int | None,
) -> list[dict[str, Any]]:
    experiment_results = [
        result
        for result in results
        if result["run"]["experiment_id"] == experiment_id
    ]

    if attempt is not None:
        selected = [
            result
            for result in experiment_results
            if result["run"].get("attempt") == attempt
        ]

        if not selected:
            raise AggregationError(
                f"Experiment {experiment_id!r} contains no attempt "
                f"{attempt} results"
            )

        return sorted(
            selected,
            key=lambda item: (
                item["run"]["adapter_id"],
                item["run"]["case_id"],
            ),
        )

    grouped: dict[
        tuple[str, str],
        list[dict[str, Any]],
    ] = defaultdict(list)

    for result in experiment_results:
        key = (
            result["run"]["case_id"],
            result["run"]["adapter_id"],
        )
        grouped[key].append(result)

    selected = []

    for group in grouped.values():
        latest = max(
            group,
            key=lambda item: (
                int(item["run"].get("attempt", 0)),
                result_timestamp(item),
            ),
        )
        selected.append(latest)

    return sorted(
        selected,
        key=lambda item: (
            item["run"]["adapter_id"],
            item["run"]["case_id"],
        ),
    )


def flatten_result(result: dict[str, Any]) -> dict[str, Any]:
    run = result["run"]
    measurements = result.get("independent_measurements", {})
    unpack = measurements.get("unpack", {})
    boot = measurements.get("boot", {})
    reachability = measurements.get("reachability", [])
    stability = measurements.get("stability", [])
    authenticity = measurements.get("authenticity", [])
    compute = measurements.get("compute_cost", {})
    residue = measurements.get("environmental_residue", {})
    remediation = measurements.get("remediation", {})
    lifecycle = nested(result, "execution", "lifecycle") or {}
    unpack_stage = candidate_stage_record(result, "unpack")
    emulate_stage = candidate_stage_record(result, "emulate")
    endpoint_stage = candidate_stage_record(
        result,
        "endpoint-discovery",
    )

    compute_wall = first_numeric(
        compute,
        {
            "wall_time_seconds",
            "duration_seconds",
            "elapsed_seconds",
            "observation_duration_seconds",
        },
    )
    compute_cpu = first_numeric(
        compute,
        {
            "cpu_seconds",
            "cpu_usage_seconds",
            "total_cpu_seconds",
            "cpu_time_seconds",
        },
    )
    peak_memory_bytes = first_numeric(
        compute,
        {
            "peak_memory_bytes",
            "memory_peak_bytes",
            "peak_rss_bytes",
            "maximum_memory_bytes",
        },
    )
    peak_memory_mb = first_numeric(
        compute,
        {
            "peak_memory_mb",
            "peak_ram_mb",
            "maximum_memory_mb",
        },
    )

    if peak_memory_mb is None and peak_memory_bytes is not None:
        peak_memory_mb = peak_memory_bytes / (1024 * 1024)

    reach_status = list_status(reachability)
    stability_status = list_status(stability)
    authenticity_status = list_status(authenticity)

    environment_status = residue.get("status")
    residue_detected = residue.get("residue_detected")

    # A detected residue is a definite cleanup failure. A clean result is
    # counted only when every configured host-state source was available.
    # partial_probe therefore remains N/A rather than being reported as 100%.
    if residue_detected is True:
        cleanup_clean = False
    elif residue_detected is False and environment_status == "clean":
        cleanup_clean = True
    else:
        cleanup_clean = None

    unpack_status = unpack.get("status")
    boot_status = boot.get("status")

    return {
        "experiment_id": run.get("experiment_id"),
        "run_id": run.get("run_id"),
        "case_id": run.get("case_id"),
        "adapter_id": run.get("adapter_id"),
        "attempt": run.get("attempt"),
        "created_at": result.get("created_at"),
        "completed_at": result.get("completed_at"),
        "overall_status": result.get("overall_status"),
        "source_path": result.get("_source_path"),
        "firmware_sha256": nested(
            result,
            "provenance",
            "firmware_sha256",
        ),
        "firmware_size_bytes": nested(
            result,
            "provenance",
            "firmware_size_bytes",
        ),
        "adapter_version": nested(
            result,
            "provenance",
            "adapter_version",
        ),
        "candidate_image_id": nested(
            result,
            "provenance",
            "candidate_image_id",
        ),
        "setup_status": nested(result, "setup", "status"),
        "build_duration_seconds": nested(
            result,
            "setup",
            "build_duration_seconds",
        ),
        "runtime_error_type": nested(
            result,
            "execution",
            "runtime_error",
            "error_type",
        ),
        "runtime_error_message": nested(
            result,
            "execution",
            "runtime_error",
            "message",
        ),
        "readiness_wait_error": nested(
            result,
            "execution",
            "readiness_wait_error",
        ),
        "endpoint_wait_error": nested(
            result,
            "execution",
            "endpoint_wait_error",
        ),
        "lifecycle_status": lifecycle.get("run_outcome"),
        "termination_mode": lifecycle.get("termination_mode"),
        "unpack_stage_outcome": unpack_stage.get("outcome"),
        "unpack_stage_seconds": unpack_stage.get("elapsed_seconds"),
        "emulate_stage_outcome": emulate_stage.get("outcome"),
        "emulate_stage_seconds": emulate_stage.get("elapsed_seconds"),
        "endpoint_discovery_stage_outcome": (
            endpoint_stage.get("outcome")
        ),
        "endpoint_discovery_stage_seconds": (
            endpoint_stage.get("elapsed_seconds")
        ),
        "unpack_status": unpack_status,
        "unpack_success": as_bool_status(unpack_status),
        "unpack_tree_sha256": unpack.get("tree_sha256"),
        "unpack_methods": "|".join(
            str(value)
            for value in unpack.get("methods", [])
        ),
        "candidate_claimed_unpack": candidate_claim_seen(
            result,
            "extraction_complete",
        ),
        "boot_status": boot_status,
        "boot_success": as_bool_status(boot_status),
        "boot_methods": "|".join(
            str(value)
            for value in boot.get("methods", [])
        ),
        "candidate_claimed_boot": candidate_claim_seen(
            result,
            "candidate_boot_reported",
        ),
        "endpoint_claim_count": endpoint_claim_count(result),
        "endpoint_claimed": endpoint_claim_count(result) > 0,
        "reachability_measurement_count": (
            len(reachability) if isinstance(reachability, list) else 0
        ),
        "stability_measurement_count": (
            len(stability) if isinstance(stability, list) else 0
        ),
        "authenticity_measurement_count": (
            len(authenticity) if isinstance(authenticity, list) else 0
        ),
        "reachability_status": reach_status,
        "service_reachable": as_bool_status(reach_status),
        "stability_status": stability_status,
        "service_stable": as_bool_status(stability_status),
        "authenticity_status": authenticity_status,
        "service_authentic": as_bool_status(authenticity_status),
        "compute_status": (
            compute.get("status")
            if isinstance(compute, dict)
            else None
        ),
        "compute_samples": (
            compute.get("samples_collected")
            if isinstance(compute, dict)
            else None
        ),
        "wall_time_seconds": compute_wall,
        "cpu_seconds": compute_cpu,
        "peak_memory_mb": (
            round(peak_memory_mb, 3)
            if peak_memory_mb is not None
            else None
        ),
        "environment_status": environment_status,
        "residue_detected": residue_detected,
        "cleanup_clean": cleanup_clean,
        "remediation_status": remediation.get("status"),
        "reachability_records": json.dumps(
            reachability,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "stability_records": json.dumps(
            stability,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "authenticity_records": json.dumps(
            authenticity,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "compute_cost_record": json.dumps(
            compute,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def percentage(
    rows: list[dict[str, Any]],
    field: str,
) -> float | None:
    """Percentage over measurements that were actually attempted.

    ``None`` means not attempted, not applicable, or unavailable and must not
    silently become a failure in the denominator.
    """
    values = [
        row.get(field)
        for row in rows
        if row.get(field) is not None
    ]

    if not values:
        return None

    return round(
        100.0 * sum(value is True for value in values) / len(values),
        1,
    )


def average(
    rows: list[dict[str, Any]],
    field: str,
) -> float | None:
    values = [
        float(row[field])
        for row in rows
        if isinstance(row.get(field), (int, float))
        and not isinstance(row.get(field), bool)
    ]

    if not values:
        return None

    return round(sum(values) / len(values), 3)


def compute_summary(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        grouped[str(row["adapter_id"])].append(row)

    summaries = []

    for adapter_id, adapter_rows in sorted(grouped.items()):
        completed_rows = [
            row
            for row in adapter_rows
            if row.get("overall_status") == "completed"
        ]
        completed = len(completed_rows)
        compute_observed = sum(
            row.get("compute_status") in {"complete", "partial"}
            for row in adapter_rows
        )

        summaries.append(
            {
                "adapter_id": adapter_id,
                "runs": len(adapter_rows),
                "completed_runs": completed,
                "non_completed_runs": len(adapter_rows) - completed,
                "unpack_success_pct": percentage(
                    adapter_rows,
                    "unpack_success",
                ),
                "boot_success_pct": percentage(
                    adapter_rows,
                    "boot_success",
                ),
                "endpoint_claim_pct": percentage(
                    completed_rows,
                    "endpoint_claimed",
                ),
                "service_reachable_pct": percentage(
                    adapter_rows,
                    "service_reachable",
                ),
                "service_authentic_pct": percentage(
                    adapter_rows,
                    "service_authentic",
                ),
                "service_stable_pct": percentage(
                    adapter_rows,
                    "service_stable",
                ),
                "cleanup_clean_pct": percentage(
                    adapter_rows,
                    "cleanup_clean",
                ),
                "compute_observed_pct": round(
                    100.0
                    * compute_observed
                    / len(adapter_rows),
                    1,
                ),
                "avg_wall_time_seconds": average(
                    adapter_rows,
                    "wall_time_seconds",
                ),
                "avg_cpu_seconds": average(
                    adapter_rows,
                    "cpu_seconds",
                ),
                "avg_peak_memory_mb": average(
                    adapter_rows,
                    "peak_memory_mb",
                ),
                "endpoint_claim_runs": sum(
                    int(row.get("endpoint_claim_count", 0)) > 0
                    for row in adapter_rows
                ),
                "reachability_measured_runs": sum(
                    int(row.get("reachability_measurement_count", 0)) > 0
                    for row in adapter_rows
                ),
                "authenticity_measured_runs": sum(
                    int(row.get("authenticity_measurement_count", 0)) > 0
                    for row in adapter_rows
                ),
                "stability_measured_runs": sum(
                    int(row.get("stability_measurement_count", 0)) > 0
                    for row in adapter_rows
                ),
                "cleanup_measured_runs": sum(
                    row.get("cleanup_clean") is not None
                    for row in adapter_rows
                ),
                "cleanup_partial_probe_count": sum(
                    row.get("environment_status") == "partial_probe"
                    for row in adapter_rows
                ),
                "setup_failed_count": sum(
                    row.get("overall_status") == "setup_failed"
                    for row in adapter_rows
                ),
                "orchestration_error_count": sum(
                    row.get("overall_status")
                    == "orchestration_error"
                    for row in adapter_rows
                ),
                "residue_detected_count": sum(
                    row.get("residue_detected") is True
                    for row in adapter_rows
                ),
            }
        )

    return summaries


def write_csv(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        path.write_text("", encoding="utf-8")
        return

    fieldnames: list[str] = []

    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    with path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def format_cell(value: Any, width: int) -> str:
    if value is None:
        text = "N/A"
    elif isinstance(value, float):
        text = f"{value:.1f}"
    else:
        text = str(value)
    return text.rjust(width)


def print_summary(summaries: list[dict[str, Any]]) -> None:
    if not summaries:
        return

    columns = [
        ("adapter_id", "Adapter", 12),
        ("runs", "Runs", 5),
        ("unpack_success_pct", "Unpack%", 8),
        ("boot_success_pct", "Boot%", 7),
        ("endpoint_claim_pct", "Endpoint%", 9),
        ("service_reachable_pct", "Reach%", 7),
        ("service_authentic_pct", "Auth%", 6),
        ("service_stable_pct", "Stable%", 8),
        ("cleanup_clean_pct", "Clean%", 7),
        ("cleanup_partial_probe_count", "Partial", 7),
        ("non_completed_runs", "NonComp", 8),
    ]

    print("\n── FIRMARBITER Adapter Contract v1 Summary ─────────────────────────")
    print(
        "  ".join(
            label.ljust(width)
            for _, label, width in columns
        )
    )
    print(
        "  ".join(
            "-" * width
            for _, _, width in columns
        )
    )

    for summary in summaries:
        print(
            "  ".join(
                format_cell(summary.get(key), width)
                for key, _, width in columns
            )
        )

    print()
    print(
        "Endpoint% is the share of completed runs that produced at least "
        "one endpoint claim. Reach/Auth/Stable percentages use only runs "
        "where that independent measurement was attempted; N/A means no "
        "eligible measurement existed. Clean% excludes partial residue "
        "probes; Partial reports their count."
    )


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate one Adapter Contract v1 FIRMARBITER experiment"
        )
    )
    parser.add_argument(
        "--results",
        default=str(DEFAULT_RESULTS_ROOT),
        help="Root containing immutable run directories",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_REPORTS_ROOT),
        help="Reports root (default: reports)",
    )
    parser.add_argument(
        "--experiment-id",
        default=None,
        help=(
            "Experiment to aggregate. Defaults to the latest experiment."
        ),
    )
    parser.add_argument(
        "--attempt",
        type=int,
        default=None,
        help=(
            "Select one exact attempt. Without this option the latest "
            "attempt per case/adapter is selected."
        ),
    )
    parser.add_argument(
        "--list-experiments",
        action="store_true",
        help="List available experiment IDs and exit",
    )
    return parser


def main() -> int:
    args = create_parser().parse_args()
    results_root = Path(args.results).expanduser().resolve()

    try:
        results = load_neutral_results(results_root)
        inventory = experiment_inventory(results)

        if args.list_experiments:
            print("Available FIRMARBITER experiments:\n")

            for item in inventory:
                print(f"  {item['experiment_id']}")
                print(f"    result files: {item['result_files']}")
                print(f"    cases:        {item['cases']}")
                print(
                    "    adapters:     "
                    + ", ".join(item["adapters"])
                )
                print(
                    f"    latest:       {item['latest_timestamp']}"
                )
                print()

            return 0

        if args.attempt is not None and args.attempt < 1:
            raise AggregationError(
                "attempt must be at least one"
            )

        experiment_id = choose_experiment(
            results,
            args.experiment_id,
        )
        selected = select_attempts(
            results,
            experiment_id=experiment_id,
            attempt=args.attempt,
        )

    except AggregationError as exc:
        print(f"FIRMARBITER aggregation error: {exc}", file=sys.stderr)
        return 2

    rows = [flatten_result(result) for result in selected]
    summaries = compute_summary(rows)

    report_directory = (
        Path(args.out).expanduser().resolve()
        / safe_token(experiment_id)
    )
    report_directory.mkdir(parents=True, exist_ok=True)

    full_path = report_directory / "full_results.csv"
    summary_path = report_directory / "summary.csv"
    selection_path = report_directory / "selection.json"

    write_csv(full_path, rows)
    write_csv(summary_path, summaries)

    selection = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "results_root": str(results_root),
        "experiment_id": experiment_id,
        "attempt_policy": (
            {"mode": "exact", "attempt": args.attempt}
            if args.attempt is not None
            else {"mode": "latest_per_case_adapter"}
        ),
        "selected_result_count": len(selected),
        "selected_runs": [
            {
                "run_id": result["run"].get("run_id"),
                "case_id": result["run"].get("case_id"),
                "adapter_id": result["run"].get("adapter_id"),
                "attempt": result["run"].get("attempt"),
                "overall_status": result.get("overall_status"),
                "source_path": result.get("_source_path"),
            }
            for result in selected
        ],
    }
    selection_path.write_text(
        json.dumps(selection, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"Experiment: {experiment_id}")
    print(f"Selected immutable results: {len(selected)}")
    print(f"Written: {full_path}")
    print(f"Written: {summary_path}")
    print(f"Written: {selection_path}")
    print_summary(summaries)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
