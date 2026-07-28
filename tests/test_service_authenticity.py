from __future__ import annotations

import hashlib
import json
import threading
import uuid
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from firmarbiter_core.adapter_registry import discover_adapters
from firmarbiter_core.docker_backend import DockerBackend
from firmarbiter_core.docker_supervisor import (
    DockerAdapterSupervisor,
)
from firmarbiter_core.probe_orchestrator import (
    IndependentProbeOrchestrator,
)
from firmarbiter_core.probes.http_snapshot import (
    capture_http_snapshot,
)
from firmarbiter_core.probes.service_authenticity import (
    evaluate_http_authenticity,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

MANIFEST_SCHEMA = (
    PROJECT_ROOT
    / "schemas"
    / "adapter-manifest-v1.schema.json"
)

REQUEST_SCHEMA = (
    PROJECT_ROOT
    / "schemas"
    / "run-request-v1.schema.json"
)

EVENT_SCHEMA = (
    PROJECT_ROOT
    / "schemas"
    / "adapter-event-v1.schema.json"
)

ADAPTER_ROOT = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "docker-probe-adapters"
)


class StaticBodyHandler(BaseHTTPRequestHandler):
    response_body = b""

    def do_GET(self) -> None:
        body = type(self).response_body

        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain",
        )
        self.send_header(
            "Content-Length",
            str(len(body)),
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(
        self,
        format: str,
        *args: Any,
    ) -> None:
        return


def start_host_service(
    body: bytes,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    StaticBodyHandler.response_body = body

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        StaticBodyHandler,
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )
    thread.start()

    return server, thread


def endpoint_event(port: int) -> dict[str, Any]:
    return {
        "event": "endpoint_reported",
        "sequence": 1,
        "endpoint": {
            "host": "127.0.0.1",
            "port": port,
            "protocol": "http",
        },
    }


def create_contract_root(
    root: Path,
    manifest_sha256: str,
) -> tuple[Path, str]:
    firmware_content = (
        b"<html><title>Firmware Administration</title></html>\n"
    )

    firmware_sha256 = hashlib.sha256(
        firmware_content
    ).hexdigest()

    input_directory = root / "input"
    input_directory.mkdir(parents=True)

    firmware_path = input_directory / "firmware"
    firmware_path.write_bytes(firmware_content)

    unique_suffix = uuid.uuid4().hex[:12]

    request = {
        "schema_version": "1.0",
        "contract_version": "1.0",
        "run": {
            "experiment_id": "authenticity-test",
            "run_id": (
                "authenticity-test.case-001."
                f"mock-service-adapter.{unique_suffix}"
            ),
            "adapter_id": "mock-service-adapter",
            "attempt": 1,
            "created_at": "2026-06-25T16:30:00Z",
            "requested_stages": [
                "emulate",
                "endpoint-discovery"
            ]
        },
        "firmware": {
            "case_id": "case-001",
            "path": "/firmarbiter/input/firmware",
            "sha256": firmware_sha256,
            "size_bytes": len(firmware_content),
            "delivery_semantics": "opaque-original-bytes",
            "read_only": True
        },
        "lifecycle": {
            "timeout_seconds": 30,
            "heartbeat_interval_seconds": 1,
            "heartbeat_timeout_seconds": 3,
            "shutdown_grace_seconds": 5
        },
        "resources": {
            "cpu_cores": 1,
            "memory_bytes": 268435456,
            "pids_limit": 128
        },
        "runtime_grants": {
            "run_as_root": False,
            "network": "host",
            "requirements": []
        },
        "paths": {
            "workspace": "/firmarbiter/work",
            "artifacts": "/firmarbiter/artifacts",
            "events": "/firmarbiter/events/events.jsonl",
            "control": "/firmarbiter/control"
        },
        "integrity": {
            "adapter_manifest_sha256": manifest_sha256,
            "experiment_manifest_sha256": "b" * 64
        }
    }

    request_path = input_directory / "request.json"

    request_path.write_text(
        json.dumps(request, indent=2) + "\n",
        encoding="utf-8",
    )

    return request_path, firmware_sha256


class ServiceAuthenticityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        adapters = discover_adapters(
            ADAPTER_ROOT,
            MANIFEST_SCHEMA,
        )

        cls.adapter = adapters["mock-service-adapter"]
        cls.backend = DockerBackend(REQUEST_SCHEMA)

        if not cls.backend.docker_available():
            raise RuntimeError(
                "Docker daemon is not available"
            )

        cls.image = cls.backend.build_adapter(
            cls.adapter
        )

    def test_persistent_host_service_is_false(self) -> None:
        body = b"Host Apache placeholder page\n"
        body_hash = hashlib.sha256(body).hexdigest()

        server, thread = start_host_service(body)

        try:
            event = endpoint_event(
                int(server.server_address[1])
            )

            active = capture_http_snapshot(event)

            # Simulate candidate shutdown while the unrelated host
            # service remains alive.
            post_shutdown = capture_http_snapshot(event)

            observation = evaluate_http_authenticity(
                active,
                post_shutdown,
                trusted_content_sha256={body_hash},
            )

            self.assertEqual(
                observation.status,
                "false",
            )

            self.assertFalse(
                observation.endpoint_disappeared_after_shutdown
            )

            # A content match cannot override lifecycle evidence.
            self.assertTrue(
                observation.trusted_content_match
            )

        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_disappearance_without_match_is_inconclusive(
        self,
    ) -> None:
        body = b"Unmatched temporary service\n"

        server, thread = start_host_service(body)
        event = endpoint_event(
            int(server.server_address[1])
        )

        active = capture_http_snapshot(event)

        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

        post_shutdown = capture_http_snapshot(
            event,
            timeout_seconds=0.5,
        )

        observation = evaluate_http_authenticity(
            active,
            post_shutdown,
            trusted_content_sha256=set(),
        )

        self.assertEqual(
            observation.status,
            "inconclusive",
        )

        self.assertTrue(
            observation.endpoint_disappeared_after_shutdown
        )

    def test_container_service_is_independently_authentic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            contract_root = Path(temporary)

            request_path, firmware_sha256 = (
                create_contract_root(
                    contract_root,
                    self.adapter.manifest_sha256,
                )
            )

            supervisor = DockerAdapterSupervisor(
                backend=self.backend,
                adapter=self.adapter,
                image=self.image,
                request_path=request_path,
                contract_root=contract_root,
                event_schema_path=EVENT_SCHEMA,
            )

            orchestrator = IndependentProbeOrchestrator()

            try:
                supervisor.start()

                endpoint = supervisor.wait_for_event(
                    "endpoint_reported",
                    timeout_seconds=10,
                )

                records = orchestrator.handle_event(endpoint)

                self.assertEqual(len(records), 1)
                self.assertEqual(
                    records[0].independent_measurement.status,
                    "true",
                )
                self.assertIsNotNone(
                    records[0].active_http_snapshot
                )
                self.assertEqual(
                    records[0].active_http_snapshot.status,
                    "captured",
                )

                self.assertTrue(
                    supervisor.container_running
                )

                supervisor.request_shutdown()

                exit_code = supervisor.wait_for_exit(
                    timeout_seconds=5,
                )

                self.assertEqual(exit_code, 0)

                authenticity_records = (
                    orchestrator.finalize_authenticity(
                        trusted_content_sha256={
                            firmware_sha256
                        },
                    )
                )

                self.assertEqual(
                    len(authenticity_records),
                    1,
                )

                authenticity = authenticity_records[
                    0
                ].independent_measurement

                self.assertEqual(
                    authenticity.status,
                    "true",
                )
                self.assertTrue(
                    authenticity.trusted_content_match
                )
                self.assertTrue(
                    authenticity
                    .endpoint_disappeared_after_shutdown
                )
                self.assertEqual(
                    authenticity.matched_sha256,
                    firmware_sha256,
                )

            finally:
                supervisor.force_terminate()
                supervisor.remove()


if __name__ == "__main__":
    unittest.main()
