#!/usr/bin/env python3
"""
tools/test_adapter.py — adapter smoke test harness.

Usage:
    python tools/test_adapter.py adapters/<your_tool>

Builds a synthetic minimal request (fake firmware bytes, short timeouts),
runs the adapter's Docker image against it, and validates the resulting
event stream against the schema. Does NOT require real firmware, real
booting, or the full coordinator — this is the fast local check an
author runs while developing a new adapter, before ever running
run_veritas.py for real.

Checks performed:
  1. adapter.yaml exists and is valid YAML with required fields
  2. Docker image builds (or already exists)
  3. Container runs to completion within a short timeout
  4. events.jsonl exists and is schema-valid (via validate_events.py)
  5. Event sequence includes the mandatory lifecycle events in order:
     adapter_started ... shutdown_started, cleanup_complete, adapter_stopped
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
SCHEMA_PATH = REPO_ROOT / "schemas" / "adapter-event-v1.schema.json"

MANDATORY_EVENT_ORDER = [
    "adapter_started",
    "shutdown_started",
    "cleanup_complete",
    "adapter_stopped",
]


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail and not condition else ""))
    return condition


def load_adapter_manifest(adapter_dir):
    manifest_path = adapter_dir / "adapter.yaml"
    if not manifest_path.exists():
        return None
    return yaml.safe_load(manifest_path.read_text())


# Fixed container-side paths. These are what request.json must contain,
# since the entrypoint reads paths from request.json and uses them as-is
# INSIDE the container — host paths are meaningless there. The harness
# mounts host tmp dirs to exactly these container paths.
CONTAINER_FIRMWARE_PATH = "/veritas/input/firmware.bin"
CONTAINER_EVENTS_PATH = "/veritas/events/events.jsonl"
CONTAINER_ARTIFACTS_DIR = "/veritas/artifacts"
CONTAINER_CONTROL_DIR = "/veritas/control"
CONTAINER_WORKSPACE_DIR = "/veritas/work"


def build_synthetic_request(work_dir, adapter_id):
    """Create fake firmware + a minimal but fully valid request.json.
    request.json contains CONTAINER-side paths; the caller is responsible
    for mounting host paths to these exact container paths."""
    firmware_path = work_dir / "firmware.bin"
    firmware_path.write_bytes(b"NOT_REAL_FIRMWARE_SYNTHETIC_SMOKE_TEST" * 100)

    events_path = work_dir / "events.jsonl"
    artifacts_dir = work_dir / "artifacts"
    control_dir = work_dir / "control"
    workspace_dir = work_dir / "workspace"
    for d in (artifacts_dir, control_dir, workspace_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Pre-create as an empty file, not a directory — otherwise Docker's
    # bind-mount creates a directory at this path when the source doesn't
    # already exist as a file.
    events_path.touch()

    request = {
        "schema_version": "1.0",
        "contract_version": "1.0",
        "run": {
            "run_id": f"smoke-test-{int(time.time())}",
            "adapter_id": adapter_id,
            "requested_stages": ["unpack", "emulate", "endpoint-discovery"],
        },
        "firmware": {
            "path": CONTAINER_FIRMWARE_PATH,
            "sha256": "0" * 64,  # synthetic, not checked by the smoke test
            "size_bytes": firmware_path.stat().st_size,
        },
        "lifecycle": {
            "heartbeat_interval_seconds": 2,
            "heartbeat_timeout_seconds": 30,
            "boot_wait_timeout_seconds": 10,
            "endpoint_wait_timeout_seconds": 10,
            "shutdown_grace_seconds": 5,
        },
        "paths": {
            "events": CONTAINER_EVENTS_PATH,
            "artifacts": CONTAINER_ARTIFACTS_DIR,
            "control": CONTAINER_CONTROL_DIR,
            "workspace": CONTAINER_WORKSPACE_DIR,
        },
    }

    request_path = work_dir / "request.json"
    request_path.write_text(json.dumps(request, indent=2))

    return request_path, events_path, firmware_path


def run_smoke_test(adapter_dir, timeout_seconds=120):
    adapter_dir = Path(adapter_dir).resolve()
    manifest = load_adapter_manifest(adapter_dir)

    if not check("adapter.yaml exists and parses", manifest is not None):
        return False

    adapter_id = manifest.get("id")
    if not check("adapter.yaml has 'id' field", bool(adapter_id)):
        return False

    image_name = f"veritas-adapter-{adapter_id}-smoketest"

    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        request_path, events_path, firmware_path = build_synthetic_request(work_dir, adapter_id)
        print(f"Synthetic request written to {request_path}")

        # Build the adapter image (fast if already cached).
        build_result = subprocess.run(
            ["docker", "build", "-t", image_name, str(adapter_dir)],
            capture_output=True, text=True,
        )
        if not check("Docker image builds", build_result.returncode == 0,
                      build_result.stderr[-500:] if build_result.returncode != 0 else ""):
            return False

        # Run the container with the synthetic request and schema mounted in,
        # matching the real paths the coordinator would use.
        run_result = subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{request_path}:/veritas/input/request.json:ro",
                "-v", f"{firmware_path}:{CONTAINER_FIRMWARE_PATH}:ro",
                "-v", f"{SCHEMA_PATH}:/veritas/schemas/adapter-event-v1.schema.json:ro",
                "-v", f"{work_dir}/artifacts:{CONTAINER_ARTIFACTS_DIR}",
                "-v", f"{work_dir}/control:{CONTAINER_CONTROL_DIR}",
                "-v", f"{work_dir}/workspace:{CONTAINER_WORKSPACE_DIR}",
                "-v", f"{events_path}:{CONTAINER_EVENTS_PATH}",
                image_name,
            ],
            capture_output=True, text=True, timeout=timeout_seconds,
        )
        container_ok = check(
            "Container ran to completion within timeout",
            run_result.returncode == 0,
            f"exit code {run_result.returncode}, stderr: {run_result.stderr[-500:]}",
        )

        events_exist = check("events.jsonl was produced", events_path.exists())
        if not events_exist:
            return False

        # Reuse the validator we already built and tested.
        sys.path.insert(0, str(REPO_ROOT / "tools"))
        from validate_events import validate_file
        total, violations = validate_file(str(events_path), str(SCHEMA_PATH))
        check("Event stream is schema-valid", violations == 0,
              f"{violations} violation(s) — see validate_events output above")

        seen_events = []
        with open(events_path) as f:
            for line in f:
                if line.strip():
                    seen_events.append(json.loads(line)["event"])

        for expected in MANDATORY_EVENT_ORDER:
            check(f"Mandatory event present: {expected}", expected in seen_events)

        # Order check: each mandatory event must appear at or after the
        # previous one's position.
        positions = [seen_events.index(e) for e in MANDATORY_EVENT_ORDER if e in seen_events]
        in_order = positions == sorted(positions)
        check("Mandatory events occur in correct order", in_order)

        return container_ok and events_exist and violations == 0 and in_order


def main():
    parser = argparse.ArgumentParser(description="VERITAS adapter smoke test harness")
    parser.add_argument("adapter_dir", help="Path to the adapter directory, e.g. adapters/emba")
    parser.add_argument("--timeout", type=int, default=120, help="Container run timeout in seconds")
    args = parser.parse_args()

    print(f"Running smoke test for adapter at {args.adapter_dir}\n")
    passed = run_smoke_test(args.adapter_dir, timeout_seconds=args.timeout)

    print()
    if passed:
        print("SMOKE TEST PASSED")
        sys.exit(0)
    else:
        print("SMOKE TEST FAILED — see failures above")
        sys.exit(1)


if __name__ == "__main__":
    main()
