from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import time
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
    # Populated only when this container was created with the
    # nested-containers requirement granted: the DinD sidecar backing it,
    # and the private per-run network joining the two. None for every
    # other container (candidates without the requirement, probe
    # sidecars, the DinD sidecar itself).
    sidecar_container_id: str | None = None
    network_name: str | None = None


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

            elif requirement == "docker-socket":
                # Sibling-container pattern (DooD): mounts the HOST's
                # real Docker socket into the candidate container.
                # This grants the candidate root-equivalent control
                # over the host's Docker daemon — a deliberate,
                # documented isolation exception, scoped only to
                # adapters that declare this requirement. See
                # create_container() for the accompanying
                # FIRMARBITER_HOST_ARTIFACTS_PATH env var, which any
                # sibling container this adapter launches needs to
                # resolve host-side bind-mount paths correctly.
                socket_path = self._require_device(
                    Path("/var/run/docker.sock")
                )
                arguments.extend(
                    [
                        "-v",
                        f"{socket_path}:/var/run/docker.sock",
                    ]
                )

            elif requirement == "nested-containers":
                # No low-level container-creation flag is added here,
                # unlike every other branch in this function. Unlike
                # docker-socket, nested-containers can't be expressed
                # as a single --device/--cap-add/-v argument — it needs
                # a second container (a DinD sidecar) created and made
                # reachable before this candidate's own `docker
                # container create` call, plus a `docker network
                # connect` step after it. That orchestration lives in
                # create_container() itself, which has access to
                # self.backend methods this function doesn't. This
                # branch exists only so the requirement isn't rejected
                # as unsupported by the fallthrough `else` below.
                pass

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

        # nested-containers must be set up *before* this candidate's own
        # `docker container create` call, since its DOCKER_HOST env var
        # has to be baked into that call's arguments — unlike
        # docker-socket (a bind mount) or every other requirement in
        # _requirement_arguments(), this can't be expressed as a flag
        # added to an already-decided argument list.
        #
        # The DinD network becomes the candidate's SOLE network when
        # this requirement is granted, overriding whatever network_mode
        # says (see the `if dind_network_name is not None` branch
        # below, near where --network gets set) — not a second
        # interface added after creation. An earlier version of this
        # tried the two-networks approach (declared network_mode as
        # primary, DinD network joined via a separate `docker network
        # connect` after creation); Docker rejects that outright for
        # "none" and "host" modes ("container cannot be connected to
        # multiple networks with one of the networks in private (none)
        # mode", confirmed live), and "isolated" isn't implemented yet
        # — so there's currently no network_mode that could accept a
        # second network anyway. A nested-containers candidate doesn't
        # need independent host-level networking of its own regardless
        # — everything it actually does over the network happens
        # against the nested daemon it creates containers on, not this
        # outer container's own network stack.
        dind_network_name: str | None = None
        dind_sidecar: CreatedContainer | None = None
        docker_host_env: str | None = None

        if "nested-containers" in runtime["requirements"]:
            dind_network_name = self.create_run_network(
                run_id
            )
            try:
                dind_sidecar = self.create_dind_sidecar(
                    network_name=dind_network_name,
                    run_id=run_id,
                )
                self.start_container(
                    dind_sidecar.container_id
                )
                if not self.container_running(
                    dind_sidecar.container_id
                ):
                    raise DockerBackendError(
                        "DinD sidecar exited during startup"
                    )
                self._wait_for_dind_ready(
                    dind_sidecar,
                    network_name=dind_network_name,
                )
            except Exception:
                # If any setup step above fails, create_container()
                # never returns a CreatedContainer — meaning the
                # caller (DockerAdapterSupervisor, run_coordinator.py)
                # never learns dind_network_name/dind_sidecar existed
                # at all, and can't clean them up themselves. Without
                # this block, a failed setup silently leaks the
                # network and/or sidecar container on every failure,
                # in production as well as in tests. Clean up whatever
                # was actually created before re-raising, rather than
                # letting the exception alone decide what gets left
                # behind.
                if dind_sidecar is not None:
                    try:
                        self.stop_container(
                            dind_sidecar.container_id
                        )
                    except Exception:
                        pass
                    try:
                        self.remove_container(
                            dind_sidecar.container_id
                        )
                    except Exception:
                        pass
                try:
                    self.remove_network(dind_network_name)
                except Exception:
                    pass
                raise
            docker_host_env = (
                f"tcp://{dind_sidecar.container_name}:2375"
            )

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

        if "docker-socket" in runtime["requirements"]:
            arguments.extend(
                [
                    "--env",
                    (
                        "FIRMARBITER_HOST_ARTIFACTS_PATH="
                        f"{artifacts_directory}"
                    ),
                ]
            )

        network_mode = runtime["network"]

        if dind_network_name is not None:
            # nested-containers overrides network_mode's usual none/
            # host/isolated choice entirely, rather than adding the
            # DinD network as a second interface — Docker's "none" and
            # "host" drivers both refuse any additional network being
            # connected to a container using them (confirmed live:
            # "container cannot be connected to multiple networks with
            # one of the networks in private (none) mode"), and
            # "isolated" isn't implemented yet. A candidate that needs
            # nested-containers doesn't need independent host-level
            # networking of its own anyway — everything Greenhouse's
            # QemuRunner actually does over the network (bridges, IPs
            # for discovered services) happens against the *nested*
            # daemon it creates containers on, not this outer
            # container's own network stack. So the DinD network
            # becomes the sole network here, and the separate
            # connect_network() step after creation is no longer
            # needed at all.
            arguments.extend(
                ["--network", dind_network_name]
            )
        elif network_mode == "none":
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

        if docker_host_env is not None:
            arguments.extend(
                ["--env", f"DOCKER_HOST={docker_host_env}"]
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
            sidecar_container_id=(
                dind_sidecar.container_id
                if dind_sidecar is not None
                else None
            ),
            network_name=dind_network_name,
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

    def create_run_network(self, run_id: str) -> str:
        # A private, per-run bridge network — not a shared namespace and
        # not a host port mapping. Scoped to exactly the one candidate
        # and DinD sidecar that need to reach each other; nothing else
        # on the host, and no other run's containers, can see this
        # network. Docker's embedded per-network DNS lets the candidate
        # reach the sidecar by container name, so no port bookkeeping
        # or IP discovery is needed.
        run_hash = hashlib.sha256(
            run_id.encode("utf-8")
        ).hexdigest()[:12]
        network_name = f"firmarbiter-dind-net-{run_hash}"

        self._run(
            [
                "network",
                "create",
                "--label",
                "firmarbiter.managed=true",
                "--label",
                f"firmarbiter.run_id={run_id}",
                network_name,
            ]
        )
        return network_name

    def disconnect_network(
        self,
        network_name: str,
        container_id: str,
    ) -> None:
        # --force detaches even a still-running or already-removed
        # container without erroring, and without requiring the
        # container to be `docker rm`'d first. Used defensively during
        # cleanup: remove_network() fails if anything is still attached,
        # and cleanup must not assume some other step (e.g. the
        # candidate's own removal) has already run — see the
        # 2026-08-18 debug flag in run_coordinator.py that disables
        # supervisor.remove() for failed-run inspection, which would
        # otherwise leave this container attached indefinitely.
        self._run(
            [
                "network",
                "disconnect",
                "--force",
                network_name,
                container_id,
            ],
            check=False,
        )

    def remove_network(self, network_name: str) -> None:
        self._run(
            ["network", "rm", network_name],
            check=False,
        )

    def connect_network(
        self,
        network_name: str,
        container_id: str,
    ) -> None:
        self._run(
            [
                "network",
                "connect",
                network_name,
                container_id,
            ]
        )

    def create_dind_sidecar(
        self,
        *,
        network_name: str,
        run_id: str,
    ) -> CreatedContainer:
        # NOTE (provenance): this references the public docker:dind tag
        # directly rather than a pinned @sha256 digest, unlike every
        # candidate base_image in the Adapter Contract's manifest schema
        # (see adapter-manifest-v1.schema.json's base_images pattern,
        # which requires a digest). That's a real gap against this
        # project's own reproducibility standard (Section III.M) — pin
        # this before treating nested-containers as fully verified, not
        # just functionally working.
        image_reference = "docker:dind"

        run_hash = hashlib.sha256(
            run_id.encode("utf-8")
        ).hexdigest()[:12]
        container_name = f"firmarbiter-dind-{run_hash}"

        arguments = [
            "container",
            "create",
            "--name",
            container_name,
            "--label",
            "firmarbiter.managed=true",
            "--label",
            "firmarbiter.role=nested-containers-sidecar",
            "--label",
            f"firmarbiter.run_id={run_id}",
            "--network",
            network_name,
            # DinD's own daemon genuinely needs --privileged (or an
            # equivalent capability set) to manage its own nested
            # containers, cgroups, and network namespaces. This is the
            # same category of documented, scoped exception as
            # docker-socket: granted only to the one purpose-built
            # sidecar backing a candidate that declared
            # nested-containers, not to candidates generally and not
            # globally.
            "--privileged",
            # docker:dind has defaulted to TLS on port 2376 since
            # Docker 19.03+; without this it will NOT be listening on
            # the plaintext 2375 this code connects to. Disabling TLS
            # here rather than setting up a cert volume is a
            # deliberate, scoped choice: the isolation boundary for
            # this connection is the private per-run network created
            # above (unreachable from the host or other runs), not
            # transport encryption — consistent with how docker-socket
            # reasons about its own exception (grant exactly what's
            # needed, scope it narrowly, don't add machinery the
            # isolation model doesn't actually need).
            "--env",
            "DOCKER_TLS_CERTDIR=",
            image_reference,
        ]

        completed = self._run(arguments)
        container_id = completed.stdout.strip()

        if not container_id:
            raise DockerBackendError(
                "Docker create returned no DinD-sidecar ID"
            )

        return CreatedContainer(
            container_id=container_id,
            container_name=container_name,
            image_id=image_reference,
        )

    def get_container_network_ip(
        self,
        container_id: str,
        network_name: str,
    ) -> str:
        # Only used for host-side readiness probing (see
        # _wait_for_dind_ready) — the host process itself is never
        # attached to the per-run network, so it has no access to
        # Docker's embedded per-network DNS the way a container on
        # that network does. Container-name-based addressing (used for
        # the candidate's own DOCKER_HOST) only resolves from inside a
        # container actually joined to the network; the host needs a
        # real IP instead.
        completed = self._run(
            [
                "inspect",
                "--format",
                (
                    "{{json (index .NetworkSettings.Networks \""
                    + network_name
                    + "\")}}"
                ),
                container_id,
            ]
        )
        network_info = json.loads(completed.stdout.strip())
        ip_address = network_info.get("IPAddress", "")

        if not ip_address:
            raise DockerBackendError(
                f"Container {container_id} has no IP address "
                f"on network {network_name} yet"
            )

        return ip_address

    def _wait_for_dind_ready(
        self,
        sidecar: CreatedContainer,
        *,
        network_name: str,
        timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        # The sidecar container reporting "running" only means the
        # dockerd *process* started, not that its daemon has finished
        # initializing and is accepting API connections yet (observed:
        # an inner `docker version` call against a freshly-started DinD
        # sidecar can fail for a few seconds before the daemon is
        # genuinely ready). Poll a real API call rather than trusting
        # container state alone.
        #
        # This probe runs on the HOST (this is a subprocess call from
        # DockerBackend itself, not from inside any container), so it
        # must use the sidecar's real IP, not its container name —
        # container-name resolution only works from inside a container
        # attached to the same network, via Docker's embedded DNS,
        # which the host has no access to. The candidate's own
        # DOCKER_HOST (built elsewhere, using the container name) is
        # correct and unaffected by this — it runs the resolution from
        # inside a container that IS attached to the network.
        deadline = time.monotonic() + timeout_seconds
        last_error: str | None = None

        while time.monotonic() < deadline:
            if not self.container_running(
                sidecar.container_id
            ):
                raise DockerBackendError(
                    "DinD sidecar exited while waiting for "
                    "its daemon to become ready"
                )

            try:
                sidecar_ip = self.get_container_network_ip(
                    sidecar.container_id, network_name
                )
            except DockerBackendError as exc:
                last_error = str(exc)
                time.sleep(poll_interval_seconds)
                continue

            docker_host = f"tcp://{sidecar_ip}:2375"

            probe = subprocess.run(
                [
                    "docker", "-H", docker_host, "version",
                    "--format", "{{.Server.Version}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if probe.returncode == 0:
                return

            last_error = probe.stderr.strip()
            time.sleep(poll_interval_seconds)

        raise DockerBackendError(
            "DinD sidecar's inner daemon did not become "
            f"ready within {timeout_seconds}s"
            + (
                f" (last error: {last_error})"
                if last_error else ""
            )
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
