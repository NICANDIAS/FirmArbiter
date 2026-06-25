from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from veritas_core.environment_residue import (
    ResidueObservation,
)


class JanitorBackend(Protocol):
    def inspect_container(
        self,
        container_id: str,
    ) -> dict[str, Any]:
        ...

    def remove_container(
        self,
        container_id: str,
    ) -> None:
        ...


class EnvironmentJanitorError(RuntimeError):
    """Raised when remediation input is invalid."""


@dataclass(frozen=True)
class RemediationAction:
    resource_type: str
    resource_identity: str
    action: str
    authorised: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RemediationActionResult:
    resource_type: str
    resource_identity: str
    action: str
    status: str
    reason: str
    error_type: str | None
    error_message: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RemediationPlan:
    schema_version: str
    run_id: str
    created_at: str
    actions: tuple[RemediationAction, ...]

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["actions"] = [
            action.to_dict()
            for action in self.actions
        ]
        return document


@dataclass(frozen=True)
class RemediationObservation:
    metric: str
    run_id: str
    status: str
    started_at: str
    completed_at: str
    attempted_actions: int
    removed_resources: int
    preserved_resources: int
    missing_resources: int
    failed_actions: int
    results: tuple[RemediationActionResult, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["results"] = [
            result.to_dict()
            for result in self.results
        ]
        return document


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _container_is_owned(
    labels: dict[str, str],
    run_id: str,
) -> bool:
    return (
        labels.get("veritas.managed") == "true"
        and labels.get("veritas.run_id") == run_id
    )


def build_remediation_plan(
    *,
    run_id: str,
    residue: ResidueObservation,
) -> RemediationPlan:
    """
    Build a conservative remediation plan.

    Only resources with independently verifiable VERITAS ownership are
    authorised for automatic removal.
    """
    if not run_id:
        raise EnvironmentJanitorError(
            "run_id must not be empty"
        )

    actions: list[RemediationAction] = []

    for container in residue.added_containers:
        owned = _container_is_owned(
            container.labels,
            run_id,
        )

        if owned:
            actions.append(
                RemediationAction(
                    resource_type="docker_container",
                    resource_identity=(
                        container.container_id
                    ),
                    action="remove",
                    authorised=True,
                    reason=(
                        "Container has the VERITAS managed label "
                        "and the exact current run identifier"
                    ),
                )
            )
        else:
            actions.append(
                RemediationAction(
                    resource_type="docker_container",
                    resource_identity=(
                        container.container_id
                    ),
                    action="preserve",
                    authorised=False,
                    reason=(
                        "Container ownership could not be proven "
                        "for the current run"
                    ),
                )
            )

    for process in residue.added_qemu_processes:
        actions.append(
            RemediationAction(
                resource_type="qemu_process",
                resource_identity=(
                    f"{process.pid}:"
                    f"{process.start_time_ticks}"
                ),
                action="preserve",
                authorised=False,
                reason=(
                    "Process appearance alone does not prove "
                    "VERITAS ownership"
                ),
            )
        )

    for interface in residue.added_tun_tap_interfaces:
        actions.append(
            RemediationAction(
                resource_type="tun_tap_interface",
                resource_identity=(
                    f"{interface.name}:"
                    f"{interface.ifindex}"
                ),
                action="preserve",
                authorised=False,
                reason=(
                    "Network-interface ownership was not "
                    "independently established"
                ),
            )
        )

    for loop_device in residue.added_loop_devices:
        actions.append(
            RemediationAction(
                resource_type="loop_device",
                resource_identity=loop_device.name,
                action="preserve",
                authorised=False,
                reason=(
                    "Loop-device ownership was not independently "
                    "established"
                ),
            )
        )

    for mapper in residue.added_device_mapper_entries:
        actions.append(
            RemediationAction(
                resource_type="device_mapper",
                resource_identity=mapper.name,
                action="preserve",
                authorised=False,
                reason=(
                    "Device-mapper ownership was not "
                    "independently established"
                ),
            )
        )

    return RemediationPlan(
        schema_version="1.0",
        run_id=run_id,
        created_at=_utc_now(),
        actions=tuple(actions),
    )


class EnvironmentJanitor:
    """
    Execute only ownership-authorised remediation actions.

    Ownership is revalidated immediately before every destructive action.
    """

    def __init__(
        self,
        *,
        backend: JanitorBackend,
        contract_root: Path,
    ) -> None:
        self.backend = backend
        self.contract_root = contract_root.resolve()

    def execute(
        self,
        plan: RemediationPlan,
    ) -> RemediationObservation:
        started_at = _utc_now()
        results: list[RemediationActionResult] = []

        for action in plan.actions:
            if (
                not action.authorised
                or action.action == "preserve"
            ):
                results.append(
                    RemediationActionResult(
                        resource_type=action.resource_type,
                        resource_identity=(
                            action.resource_identity
                        ),
                        action=action.action,
                        status="preserved",
                        reason=action.reason,
                        error_type=None,
                        error_message=None,
                    )
                )
                continue

            if (
                action.resource_type
                != "docker_container"
                or action.action != "remove"
            ):
                results.append(
                    RemediationActionResult(
                        resource_type=action.resource_type,
                        resource_identity=(
                            action.resource_identity
                        ),
                        action=action.action,
                        status="preserved",
                        reason=(
                            "No ownership-safe executor exists "
                            "for this resource type"
                        ),
                        error_type=None,
                        error_message=None,
                    )
                )
                continue

            container_id = action.resource_identity

            try:
                inspection = self.backend.inspect_container(
                    container_id
                )

            except Exception as exc:
                results.append(
                    RemediationActionResult(
                        resource_type="docker_container",
                        resource_identity=container_id,
                        action="remove",
                        status="missing",
                        reason=(
                            "Container no longer existed when "
                            "remediation began"
                        ),
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                )
                continue

            current_id = inspection.get("Id")
            configuration = inspection.get(
                "Config",
                {},
            )

            if not isinstance(configuration, dict):
                configuration = {}

            labels = configuration.get("Labels")

            if not isinstance(labels, dict):
                labels = {}

            normalised_labels = {
                str(key): str(value)
                for key, value in labels.items()
            }

            if current_id != container_id:
                results.append(
                    RemediationActionResult(
                        resource_type="docker_container",
                        resource_identity=container_id,
                        action="remove",
                        status="preserved",
                        reason=(
                            "Container identity changed before "
                            "remediation"
                        ),
                        error_type=None,
                        error_message=None,
                    )
                )
                continue

            if not _container_is_owned(
                normalised_labels,
                plan.run_id,
            ):
                results.append(
                    RemediationActionResult(
                        resource_type="docker_container",
                        resource_identity=container_id,
                        action="remove",
                        status="preserved",
                        reason=(
                            "VERITAS ownership labels were absent "
                            "or did not match the current run"
                        ),
                        error_type=None,
                        error_message=None,
                    )
                )
                continue

            try:
                self.backend.remove_container(
                    container_id
                )

                results.append(
                    RemediationActionResult(
                        resource_type="docker_container",
                        resource_identity=container_id,
                        action="remove",
                        status="removed",
                        reason=(
                            "Ownership was revalidated and the "
                            "managed container was removed"
                        ),
                        error_type=None,
                        error_message=None,
                    )
                )

            except Exception as exc:
                results.append(
                    RemediationActionResult(
                        resource_type="docker_container",
                        resource_identity=container_id,
                        action="remove",
                        status="failed",
                        reason=(
                            "Container removal failed after "
                            "ownership validation"
                        ),
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                )

        removed = sum(
            result.status == "removed"
            for result in results
        )
        preserved = sum(
            result.status == "preserved"
            for result in results
        )
        missing = sum(
            result.status == "missing"
            for result in results
        )
        failed = sum(
            result.status == "failed"
            for result in results
        )

        if failed > 0:
            status = "partial_failure"
            reason = (
                "One or more authorised remediation actions "
                "failed"
            )
        elif preserved > 0:
            status = "manual_review_required"
            reason = (
                "Ownership-safe remediation completed, but one "
                "or more unowned resources were preserved"
            )
        elif removed > 0:
            status = "remediated"
            reason = (
                "All ownership-authorised resources were removed"
            )
        elif missing > 0:
            status = "already_clean"
            reason = (
                "Authorised resources no longer existed when "
                "remediation began"
            )
        else:
            status = "no_action_required"
            reason = (
                "The residue observation required no automatic "
                "remediation"
            )

        observation = RemediationObservation(
            metric="environment_remediation",
            run_id=plan.run_id,
            status=status,
            started_at=started_at,
            completed_at=_utc_now(),
            attempted_actions=len(results),
            removed_resources=removed,
            preserved_resources=preserved,
            missing_resources=missing,
            failed_actions=failed,
            results=tuple(results),
            reason=reason,
        )

        self._persist(
            plan=plan,
            observation=observation,
        )

        return observation

    def _persist(
        self,
        *,
        plan: RemediationPlan,
        observation: RemediationObservation,
    ) -> None:
        artifact_directory = (
            self.contract_root / "artifacts"
        )
        artifact_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        documents = {
            "remediation-plan.json": plan.to_dict(),
            "remediation-observation.json": (
                observation.to_dict()
            ),
        }

        for filename, document in documents.items():
            output_path = artifact_directory / filename
            temporary_path = output_path.with_suffix(
                ".tmp"
            )

            temporary_path.write_text(
                json.dumps(
                    document,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            temporary_path.replace(output_path)
