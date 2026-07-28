from __future__ import annotations

import json
from typing import Any

from firmarbiter_core.docker_backend import (
    BuiltProbeImage,
    CreatedContainer,
    DockerBackend,
    DockerBackendError,
)
from firmarbiter_core.probes.http_snapshot import (
    HttpServiceSnapshot,
)
from firmarbiter_core.probes.service_reachability import (
    ReachabilityObservation,
)


class DockerNamespaceProbeError(RuntimeError):
    """Raised when neutral namespace probing cannot continue."""


class DockerNamespaceProbeSidecar:
    """
    Long-lived neutral measurement container sharing only the candidate
    container's network namespace.

    The sidecar stays alive after the candidate container stops so FIRMARBITER
    can perform post-shutdown authenticity checks in the same namespace.
    """

    def __init__(
        self,
        *,
        backend: DockerBackend,
        image: BuiltProbeImage,
        candidate_container_id: str,
        run_id: str,
    ) -> None:
        self.backend = backend
        self.image = image
        self.candidate_container_id = (
            candidate_container_id
        )
        self.run_id = run_id
        self.container: CreatedContainer | None = None

    @property
    def container_id(self) -> str:
        if self.container is None:
            raise DockerNamespaceProbeError(
                "Probe sidecar has not been started"
            )
        return self.container.container_id

    def start(self) -> None:
        if self.container is not None:
            raise DockerNamespaceProbeError(
                "Probe sidecar has already been started"
            )

        self.container = (
            self.backend.create_network_probe_sidecar(
                candidate_container_id=(
                    self.candidate_container_id
                ),
                image=self.image,
                run_id=self.run_id,
            )
        )
        self.backend.start_container(
            self.container.container_id
        )

        if not self.backend.container_running(
            self.container.container_id
        ):
            raise DockerNamespaceProbeError(
                "Neutral network-probe sidecar exited "
                "during startup"
            )

    def _execute(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        try:
            return self.backend.exec_container_json(
                container_id=self.container_id,
                arguments=[
                    "/usr/bin/python3",
                    "/opt/firmarbiter-probe/worker.py",
                    operation,
                    json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ],
                timeout=max(
                    5.0,
                    timeout_seconds + 5.0,
                ),
            )
        except DockerBackendError as exc:
            raise DockerNamespaceProbeError(
                str(exc)
            ) from exc

    def probe_endpoint_event(
        self,
        event: dict[str, Any],
        *,
        timeout_seconds: float = 2.0,
        attempts: int = 1,
        retry_delay_seconds: float = 0.1,
    ) -> ReachabilityObservation:
        endpoint = event.get("endpoint")

        if not isinstance(endpoint, dict):
            raise DockerNamespaceProbeError(
                "Endpoint event has no endpoint object"
            )

        document = self._execute(
            "reachability",
            {
                "endpoint": endpoint,
                "timeout_seconds": timeout_seconds,
                "attempts": attempts,
                "retry_delay_seconds": (
                    retry_delay_seconds
                ),
            },
            timeout_seconds=(
                timeout_seconds * max(1, attempts)
                + retry_delay_seconds
                * max(0, attempts - 1)
            ),
        )
        return ReachabilityObservation(**document)

    def capture_http_snapshot(
        self,
        event: dict[str, Any],
        *,
        timeout_seconds: float = 3.0,
        max_body_bytes: int = 1024 * 1024,
    ) -> HttpServiceSnapshot:
        endpoint = event.get("endpoint")

        if not isinstance(endpoint, dict):
            raise DockerNamespaceProbeError(
                "Endpoint event has no endpoint object"
            )

        document = self._execute(
            "http_snapshot",
            {
                "endpoint": endpoint,
                "timeout_seconds": timeout_seconds,
                "max_body_bytes": max_body_bytes,
            },
            timeout_seconds=timeout_seconds,
        )
        return HttpServiceSnapshot(**document)

    def close(self) -> None:
        if self.container is None:
            return

        container_id = self.container.container_id

        try:
            if self.backend.container_running(
                container_id
            ):
                self.backend.stop_container(
                    container_id,
                    timeout_seconds=2,
                )
        finally:
            self.backend.remove_container(
                container_id
            )
            self.container = None
