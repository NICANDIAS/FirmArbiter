#!/usr/bin/env python3
"""
tools/manual_test_nested_containers.py

Goes one real step further than the existing automated test
(tests/test_nested_containers_lifecycle.py's
test_nested_containers_reaches_real_daemon), which only proves the
DinD sidecar comes up and answers `docker version` -- not that a real
child container can actually be created and run inside it. That gap
has been called out repeatedly tonight as the one thing keeping
nested-containers at EXPERIMENTAL rather than SUPPORTED in
firmarbiter_core/runtime_requirements.py.

This script uses FirmArbiter's own real DockerBackend methods --
create_run_network(), create_dind_sidecar(), start_container(),
_wait_for_dind_ready() -- exactly the same ones a real candidate run
would use, then goes further: issues a REAL `docker run` against the
nested daemon and checks for real output, not just a version string.

Cleans up everything it creates, in reverse order, even on failure.

Usage:
    ./python tools/manual_test_nested_containers.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from firmarbiter_core.docker_backend import (  # noqa: E402
    DockerBackend,
    DockerBackendError,
)

RUN_REQUEST_SCHEMA = PROJECT_ROOT / "schemas" / "run-request-v1.schema.json"


def main() -> int:
    backend = DockerBackend(RUN_REQUEST_SCHEMA)
    run_id = "manual-nested-test"

    network_name = None
    sidecar = None

    try:
        print("[1/5] Creating per-run network...")
        network_name = backend.create_run_network(run_id)
        print(f"      -> {network_name}")

        print("[2/5] Creating DinD sidecar...")
        sidecar = backend.create_dind_sidecar(
            network_name=network_name, run_id=run_id,
        )
        print(f"      -> {sidecar.container_name} ({sidecar.container_id[:12]})")

        print("[3/5] Starting sidecar and waiting for its inner daemon...")
        backend.start_container(sidecar.container_id)
        backend._wait_for_dind_ready(sidecar, network_name=network_name)
        print("      -> inner daemon is ready")

        print("[4/5] THE REAL TEST: running a genuine container INSIDE the nested daemon...")
        sidecar_ip = backend.get_container_network_ip(
            sidecar.container_id, network_name,
        )
        docker_host = f"tcp://{sidecar_ip}:2375"

        result = subprocess.run(
            [
                "docker", "-H", docker_host, "run", "--rm",
                "alpine:3.20", "sh", "-c",
                "echo NESTED_CONTAINER_REALLY_RAN && hostname",
            ],
            capture_output=True, text=True, timeout=120,
        )

        print(f"      exit code: {result.returncode}")
        print(f"      stdout:\n{result.stdout}")
        if result.stderr:
            print(f"      stderr:\n{result.stderr}")

        if result.returncode == 0 and "NESTED_CONTAINER_REALLY_RAN" in result.stdout:
            print(
                "\n[5/5] CONFIRMED: a real container genuinely ran INSIDE "
                "the nested daemon, with real output captured back on "
                "the host. This is the specific gap the automated test "
                "doesn't cover -- if you're reading a clean pass here, "
                "that gap is now closed for real, not just asserted."
            )
            return 0
        else:
            print(
                "\n[5/5] DID NOT CONFIRM: the sidecar came up and "
                "answered connectivity checks, but running a real "
                "container inside it did not succeed as expected. This "
                "is real, useful information -- nested-containers "
                "should stay EXPERIMENTAL, and whatever printed above "
                "is the actual reason why."
            )
            return 1

    except DockerBackendError as exc:
        print(f"\nFAILED with a DockerBackendError: {exc}")
        return 1

    finally:
        print("\nCleaning up...")
        if sidecar is not None:
            try:
                backend.stop_container(sidecar.container_id)
            except Exception as exc:
                print(f"  (non-fatal) stop_container failed: {exc}")
            try:
                backend.remove_container(sidecar.container_id)
                print(f"  removed sidecar {sidecar.container_name}")
            except Exception as exc:
                print(f"  (non-fatal) remove_container failed: {exc}")
        if network_name is not None:
            try:
                backend.remove_network(network_name)
                print(f"  removed network {network_name}")
            except Exception as exc:
                print(f"  (non-fatal) remove_network failed: {exc}")


if __name__ == "__main__":
    sys.exit(main())
