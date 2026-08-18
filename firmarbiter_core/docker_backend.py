from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import fcntl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from jsonschema import Draft202012Validator

from firmarbiter_core.adapter_registry import AdapterRecord


class DockerBackendError(RuntimeError):
    """Raised when neutral Docker execution cannot continue."""


class RuntimeRequirementError(DockerBackendError):
    """Raised when an authorised runtime requirement is unavailable."""


@dataclass(frozen=True)
class BuiltAdapterImage:
    adapter_id: str
    reference: str
    image_id: str
    repo_digests: tuple[str, ...]


@dataclass(frozen=True)
class BuiltProbeImage:
    reference: str
    image_id: str
    repo_digests: tuple[str, ...]
    platform: str


@dataclass(frozen=True)
class CreatedContainer:
    container_id: str
    container_name: str
    image_id: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def _safe_container_component(value: str) -> str:
    cleaned = "".join(
        character.lower()
        if character.isalnum()
        else "-"
        for character in value
    ).strip("-")

    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")

    return cleaned[:40] or "run"


class DockerBackend:
    """
    Candidate-neutral Docker build and runtime backend.

    Candidate identifiers are treated only as manifest and run data.
    """

    def __init__(self, run_request_schema_path: Path) -> None:
        self.run_request_schema_path = (
            run_request_schema_path.resolve()
        )

        try:
            schema = json.loads(
                self.run_request_schema_path.read_text(
                    encoding="utf-8"
                )
            )
        except FileNotFoundError as exc:
            raise DockerBackendError(
                f"Run-request schema not found: "
                f"{self.run_request_schema_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise DockerBackendError(
                f"Invalid run-request schema: {exc}"
            ) from exc

        Draft202012Validator.check_schema(schema)

        self._request_validator = Draft202012Validator(
            schema,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )
        self._built_image_cache: dict[
            str,
            BuiltAdapterImage,
        ] = {}
        self._neutral_probe_image: (
            BuiltProbeImage | None
        ) = None

    def _run(
        self,
        arguments: Sequence[str],
        *,
        check: bool = True,
        timeout: float = 300,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                ["docker", *arguments],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DockerBackendError(
                "Docker CLI was not found"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerBackendError(
                f"Docker command timed out: "
                f"docker {' '.join(arguments)}"
            ) from exc

        if check and completed.returncode != 0:
            raise DockerBackendError(
                f"Docker command failed with code "
                f"{completed.returncode}:\n"
                f"docker {' '.join(arguments)}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )

        return completed

    def docker_available(self) -> bool:
        completed = self._run(
            ["info", "--format", "{{json .ServerVersion}}"],
            check=False,
            timeout=20,
        )
        return completed.returncode == 0

    def build_adapter(
        self,
        adapter: AdapterRecord,
    ) -> BuiltAdapterImage:
        cached = self._built_image_cache.get(
            adapter.manifest_sha256
        )

        if cached is not None:
            return cached

        build = adapter.manifest["build"]

        context_path = (
            adapter.package_dir / build["context"]
        ).resolve()

        dockerfile_path = (
            context_path / build["dockerfile"]
        ).resolve()

        image_reference = (
            f"firmarbiter-adapter-{adapter.adapter_id}:"
            f"{adapter.manifest['adapter']['version']}"
        )

        self._run(
            [
                "build",
                "--pull",
                "--platform",
                build["platform"],
                "--file",
                str(dockerfile_path),
                "--tag",
                image_reference,
                str(context_path),
            ],
            timeout=1800,
        )

        image_data = self.inspect_image(image_reference)

        image_id = image_data.get("Id")

        if not isinstance(image_id, str):
            raise DockerBackendError(
                f"Docker did not return an image ID for "
                f"{image_reference}"
            )

        repo_digests = tuple(
            item
            for item in image_data.get("RepoDigests", [])
            if isinstance(item, str)
        )

        built_image = BuiltAdapterImage(
            adapter_id=adapter.adapter_id,
            reference=image_reference,
            image_id=image_id,
            repo_digests=repo_digests,
        )
        self._built_image_cache[
            adapter.manifest_sha256
        ] = built_image
        return built_image

    def build_neutral_probe_image(
        self,
    ) -> BuiltProbeImage:
        if self._neutral_probe_image is not None:
            return self._neutral_probe_image
        context_path = Path(__file__).resolve().parent
        dockerfile_path = (
            context_path
            / "probe_image"
            / "Dockerfile"
        )
        image_reference = (
            "firmarbiter-neutral-network-probe:1.0.0"
        )
        lock_path = context_path / ".probe-build.lock"
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                self._run(
                    [
                        "build",
                        "--pull",
                        "--file",
                        str(dockerfile_path),
                        "--tag",
                        image_reference,
                        str(context_path),
                    ],
                    timeout=1200,
                )
                image_data = self.inspect_image(image_reference)
                image_id = image_data.get("Id")
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

        if not isinstance(image_id, str):
            raise DockerBackendError(
                "Docker did not return an image ID for "
                f"{image_reference}"
            )

        architecture = image_data.get(
            "Architecture"
        )
        operating_system = image_data.get("Os")

        if (
            not isinstance(architecture, str)
            or not isinstance(operating_system, str)
        ):
            raise DockerBackendError(
                "Docker did not return the neutral probe "
                "image platform"
            )

        repo_digests = tuple(
            item
            for item in image_data.get(
                "RepoDigests",
                [],
            )
            if isinstance(item, str)
        )

        built = BuiltProbeImage(
            reference=image_reference,
            image_id=image_id,
            repo_digests=repo_digests,
            platform=(
                f"{operating_system}/{architecture}"
            ),
        )
        self._neutral_probe_image = built
        return built

    def inspect_image(
        self,
        image_reference: str,
    ) -> dict[str, Any]:
        completed = self._run(
            ["image", "inspect", image_reference]
        )

        try:
            documents = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DockerBackendError(
                f"Invalid Docker image-inspect response: {exc}"
            ) from exc

        if (
            not isinstance(documents, list)
            or not documents
            or not isinstance(documents[0], dict)
        ):
            raise DockerBackendError(
                "Docker image inspect returned no image"
            )

        return documents[0]

    def load_and_validate_request(
        self,
        request_path: Path,
        adapter: AdapterRecord,
    ) -> dict[str, Any]:
        try:
            request = json.loads(
                request_path.read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise DockerBackendError(
                f"Run request not found: {request_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise DockerBackendError(
                f"Invalid run-request JSON: {exc}"
            ) from exc

        errors = sorted(
            self._request_validator.iter_errors(request),
            key=lambda error: list(error.absolute_path),
        )

        if errors:
            messages = []

            for error in errors:
                location = ".".join(
                    str(part)
                    for part in error.absolute_path
                )
                messages.append(
                    f"{location or '<root>'}: {error.message}"
                )

            raise DockerBackendError(
                "Run-request validation failed:\n  - "
                + "\n  - ".join(messages)
            )

        if request["run"]["adapter_id"] != adapter.adapter_id:
            raise DockerBackendError(
                "Run-request adapter_id does not match the "
                "validated adapter manifest"
            )

        requested_stages = set(
            request["run"]["requested_stages"]
        )

        supported_stages = set(
            adapter.manifest["capabilities"]["stages"]
        )

        unsupported_stages = (
            requested_stages - supported_stages
        )

        if unsupported_stages:
            raise DockerBackendError(
                "Run request contains unsupported stages: "
                + ", ".join(sorted(unsupported_stages))
            )

        manifest_runtime = adapter.manifest["runtime"]
        runtime_grants = request["runtime_grants"]

        if (
            runtime_grants["run_as_root"]
            != manifest_runtime["run_as_root"]
        ):
            raise DockerBackendError(
                "run_as_root grant does not match the "
                "validated adapter manifest"
            )

        if (
            runtime_grants["network"]
            != manifest_runtime["network"]
        ):
            raise DockerBackendError(
                "Network grant does not match the validated "
                "adapter manifest"
            )

        if set(runtime_grants["requirements"]) != set(
            manifest_runtime["requirements"]
        ):
            raise DockerBackendError(
                "Runtime requirement grants do not match the "
                "validated adapter manifest"
            )

        firmware_path = request_path.parent / "firmware"

        if not firmware_path.is_file():
            raise DockerBackendError(
                f"Canonical firmware object not found: "
                f"{firmware_path}"
            )

        actual_size = firmware_path.stat().st_size
        actual_hash = _sha256_file(firmware_path)

        if actual_size != request["firmware"]["size_bytes"]:
            raise DockerBackendError(
                "Canonical firmware size does not match the "
                "run request"
            )

        if actual_hash != request["firmware"]["sha256"]:
            raise DockerBackendError(
                "Canonical firmware SHA-256 does not match "
                "the run request"
            )

        return request

    def _require_device(self, path: Path) -> str:
        if not path.exists():
            raise RuntimeRequirementError(
                f"Required host device is unavailable: {path}"
            )

        return str(path)

    def _requirement_arguments(
        self,
        requirements: list[str],
    ) -> list[str]:
        arguments: list[str] = []
        is_privileged = "full-privileged" in requirements

        for requirement in sorted(requirements):
            if requirement == "kvm":
                device = self._require_device(Path("/dev/kvm"))
                arguments.extend(["--device", device])

            elif requirement == "tun-tap":
                device = self._require_device(
                    Path("/dev/net/tun")
                )
                arguments.extend(["--device", device])

            elif requirement == "device-mapper":
                device = self._require_device(
                    Path("/dev/mapper/control")
                )
                arguments.extend(["--device", device])

            elif requirement == "net-admin":
                arguments.extend(
                    ["--cap-add", "NET_ADMIN"]
                )

            elif requirement == "ptrace":
                arguments.extend(
                    ["--cap-add", "SYS_PTRACE"]
                )

            elif requirement == "full-privileged":
                arguments.append("--privileged")

            elif requirement == "loop-devices":
                loop_control = self._require_device(
                    Path("/dev/loop-control")
                )
                arguments.extend(
                    ["--device", loop_control]
                )
                if not is_privileged:
                    # Grant access to the entire loop device major
                    # number (7) via a device-cgroup rule, rather
                    # than attaching a fixed snapshot of /dev/loopN
                    # nodes that exist at container-creation time.
                    # A fixed snapshot fails whenever every existing
                    # loop device is already in use by the host
                    # (observed: snapd-heavy Ubuntu installs
                    # commonly occupy every /dev/loopN with mounted
                    # .snap files) or whenever a candidate allocates
                    # a new loop device via losetup after the
                    # container has already started, since a
                    # host-side node created after attachment is
                    # never visible inside the container under the
                    # fixed-list model.
                    #
                    # This rule is deliberately skipped when
                    # full-privileged is also requested: a
                    # privileged container already has unrestricted
                    # device access, and adding an explicit
                    # device-cgroup rule on top of --privileged was
                    # observed to narrow rather than extend that
                    # access on this host's Docker/cgroup version,
                    # causing FIRMADYNE's own filesystem-extraction
                    # stage to fail outright (exit 1, no stderr)
                    # despite the container starting successfully.
                    arguments.extend(
                        [
                            "--device-cgroup-rule",
                            "c 7:* rmw",
                        ]
                    )

            elif requirement == "nested-containers":
                raise RuntimeRequirementError(
                    "nested-containers is declared by the "
                    "contract but is not implemented by the "
                    "v1 Docker backend"
                )

            else:
                raise RuntimeRequirementError(
                    f"Unsupported runtime requirement: "
                    f"{requirement}"
                )

        return arguments

    def create_container(
        self,
        adapter: AdapterRecord,
        image: BuiltAdapterImage,
        request_path: Path,
        contract_root: Path,
    ) -> CreatedContainer:
        request_path = request_path.resolve()
        contract_root = contract_root.resolve()

        request = self.load_and_validate_request(
            request_path,
            adapter,
        )

        input_directory = contract_root / "input"
        work_directory = contract_root / "work"
        artifacts_directory = contract_root / "artifacts"
        events_directory = contract_root / "events"
        control_directory = contract_root / "control"

        for directory in (
            work_directory,
            artifacts_directory,
            events_directory,
            control_directory,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        run_id = request["run"]["run_id"]
        run_hash = hashlib.sha256(
            run_id.encode("utf-8")
        ).hexdigest()[:12]

        container_name = (
            "firmarbiter-"
            + _safe_container_component(
                request["run"]["adapter_id"]
            )
            + "-"
            + run_hash
        )

        resources = request["resources"]
        runtime = request["runtime_grants"]

        arguments = [
            "container",
            "create",
            "--name",
            container_name,
            "--init",
            "--label",
            "firmarbiter.managed=true",
            "--label",
            f"firmarbiter.run_id={run_id}",
            "--label",
            (
                "firmarbiter.experiment_id="
                f"{request['run']['experiment_id']}"
            ),
            "--label",
            (
                "firmarbiter.adapter_id="
                f"{request['run']['adapter_id']}"
            ),
            "--cpus",
            str(resources["cpu_cores"]),
            "--memory",
            str(resources["memory_bytes"]),
            "--memory-swap",
            str(resources["memory_bytes"]),
            "--pids-limit",
            str(resources["pids_limit"]),
            "--mount",
            (
                f"type=bind,src={input_directory},"
                "dst=/firmarbiter/input,readonly"
            ),
            "--mount",
            (
                f"type=bind,src={work_directory},"
                "dst=/firmarbiter/work"
            ),
            "--mount",
            (
                f"type=bind,src={artifacts_directory},"
                "dst=/firmarbiter/artifacts"
            ),
            "--mount",
            (
                f"type=bind,src={events_directory},"
                "dst=/firmarbiter/events"
            ),
            "--mount",
            (
                f"type=bind,src={control_directory},"
                "dst=/firmarbiter/control"
            ),
        ]

        network_mode = runtime["network"]

        if network_mode == "none":
            arguments.extend(["--network", "none"])
        elif network_mode == "host":
            arguments.extend(["--network", "host"])
        elif network_mode == "isolated":
            raise RuntimeRequirementError(
                "Per-run isolated Docker networking has not "
                "yet been implemented"
            )
        else:
            raise RuntimeRequirementError(
                f"Unsupported network mode: {network_mode}"
            )

        if not runtime["run_as_root"]:
            arguments.extend(
                [
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                ]
            )

        arguments.extend(
            self._requirement_arguments(
                runtime["requirements"]
            )
        )

        arguments.extend(
            [
                image.reference,
                "/firmarbiter/input/request.json",
            ]
        )

        completed = self._run(arguments)

        container_id = completed.stdout.strip()

        if not container_id:
            raise DockerBackendError(
                "Docker create returned no container ID"
            )

        return CreatedContainer(
            container_id=container_id,
            container_name=container_name,
            image_id=image.image_id,
        )

    def create_network_probe_sidecar(
        self,
        *,
        candidate_container_id: str,
        image: BuiltProbeImage,
        run_id: str,
    ) -> CreatedContainer:
        run_hash = hashlib.sha256(
            run_id.encode("utf-8")
        ).hexdigest()[:12]
        container_name = f"firmarbiter-probe-{run_hash}"

        arguments = [
            "container",
            "create",
            "--name",
            container_name,
            "--init",
            "--label",
            "firmarbiter.managed=true",
            "--label",
            "firmarbiter.role=neutral-network-probe",
            "--label",
            f"firmarbiter.run_id={run_id}",
            "--network",
            f"container:{candidate_container_id}",
            "--read-only",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            "0.25",
            "--memory",
            str(128 * 1024 * 1024),
            "--memory-swap",
            str(128 * 1024 * 1024),
            "--pids-limit",
            "64",
            "--entrypoint",
            "/usr/bin/python3",
            image.image_id,
            "-c",
            (
                "import signal,sys,time;"
                "signal.signal(signal.SIGTERM,lambda *_:sys.exit(0));"
                "time.sleep(604800)"
            ),
        ]

        completed = self._run(arguments)
        container_id = completed.stdout.strip()

        if not container_id:
            raise DockerBackendError(
                "Docker create returned no probe-sidecar ID"
            )

        return CreatedContainer(
            container_id=container_id,
            container_name=container_name,
            image_id=image.image_id,
        )

    def exec_container_json(
        self,
        *,
        container_id: str,
        arguments: Sequence[str],
        timeout: float,
    ) -> dict[str, Any]:
        completed = self._run(
            [
                "container",
                "exec",
                container_id,
                *arguments,
            ],
            check=False,
            timeout=timeout,
        )

        if completed.returncode != 0:
            raise DockerBackendError(
                "Neutral namespace probe failed with code "
                f"{completed.returncode}:\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )

        lines = [
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip()
        ]

        if not lines:
            raise DockerBackendError(
                "Neutral namespace probe returned no JSON"
            )

        try:
            document = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise DockerBackendError(
                f"Neutral namespace probe returned invalid JSON: "
                f"{exc}"
            ) from exc

        if not isinstance(document, dict):
            raise DockerBackendError(
                "Neutral namespace probe result was not an object"
            )

        return document

    def start_container(self, container_id: str) -> None:
        self._run(
            ["container", "start", container_id]
        )

    def inspect_container(
        self,
        container_id: str,
    ) -> dict[str, Any]:
        completed = self._run(
            ["container", "inspect", container_id]
        )

        try:
            documents = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DockerBackendError(
                f"Invalid container-inspect response: {exc}"
            ) from exc

        if (
            not isinstance(documents, list)
            or not documents
            or not isinstance(documents[0], dict)
        ):
            raise DockerBackendError(
                "Docker container inspect returned no container"
            )

        return documents[0]

    def container_running(self, container_id: str) -> bool:
        state = self.inspect_container(container_id)["State"]
        return bool(state.get("Running"))

    def exit_code(self, container_id: str) -> int:
        state = self.inspect_container(container_id)["State"]
        return int(state.get("ExitCode", -1))

    def stop_container(
        self,
        container_id: str,
        timeout_seconds: int = 2,
    ) -> None:
        self._run(
            [
                "container",
                "stop",
                "--time",
                str(timeout_seconds),
                container_id,
            ],
            check=False,
            timeout=timeout_seconds + 10,
        )

    def container_stats_snapshot(
        self,
        container_id: str,
    ) -> dict[str, Any]:
        """
        Return one Docker-generated resource snapshot.

        The backend returns raw Docker fields. Candidate-neutral parsing and
        aggregation are performed by the compute-cost sampler.
        """
        completed = self._run(
            [
                "container",
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
                container_id,
            ],
            check=False,
            timeout=30,
        )

        if completed.returncode != 0:
            raise DockerBackendError(
                f"Could not collect container statistics for "
                f"{container_id}:\n{completed.stderr}"
            )

        lines = [
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip()
        ]

        if not lines:
            raise DockerBackendError(
                f"Docker returned no statistics for container "
                f"{container_id}"
            )

        try:
            document = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise DockerBackendError(
                f"Invalid Docker statistics response: {exc}"
            ) from exc

        if not isinstance(document, dict):
            raise DockerBackendError(
                "Docker statistics response was not a JSON object"
            )

        return document

    def kill_container(self, container_id: str) -> None:
        self._run(
            ["container", "kill", container_id],
            check=False,
            timeout=20,
        )

    def container_logs(
        self,
        container_id: str,
    ) -> tuple[str, str]:
        completed = self._run(
            ["container", "logs", container_id],
            check=False,
            timeout=60,
        )
        return completed.stdout, completed.stderr

def remove_container(self, container_id: str) -> None:
    self._run(
        ["container", "rm", "--force", container_id],
        check=False,
        timeout=30,
    )
