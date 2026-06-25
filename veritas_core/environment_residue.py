from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


class EnvironmentProbeError(RuntimeError):
    """Raised when environmental residue input is invalid."""


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    start_time_ticks: int
    executable: str | None
    command: tuple[str, ...]

    @property
    def identity(self) -> tuple[int, int]:
        return self.pid, self.start_time_ticks


@dataclass(frozen=True)
class NetworkInterfaceRecord:
    name: str
    ifindex: int | None
    kind: str | None
    tun_flags: str | None

    @property
    def identity(self) -> tuple[str, int | None]:
        return self.name, self.ifindex


@dataclass(frozen=True)
class LoopDeviceRecord:
    name: str
    backing_file: str | None
    offset_bytes: int | None
    autoclear: bool | None

    @property
    def identity(self) -> tuple[str, str | None, int | None]:
        return self.name, self.backing_file, self.offset_bytes


@dataclass(frozen=True)
class DeviceMapperRecord:
    name: str
    uuid: str | None
    major: int | None
    minor: int | None

    @property
    def identity(self) -> tuple[str, str | None, int | None, int | None]:
        return self.name, self.uuid, self.major, self.minor


@dataclass(frozen=True)
class ContainerRecord:
    container_id: str
    name: str
    image: str
    state: str
    labels: dict[str, str]

    @property
    def identity(self) -> str:
        return self.container_id


@dataclass(frozen=True)
class HostEnvironmentSnapshot:
    observed_at: str
    qemu_processes: tuple[ProcessRecord, ...]
    tun_tap_interfaces: tuple[NetworkInterfaceRecord, ...]
    loop_devices: tuple[LoopDeviceRecord, ...]
    device_mapper_entries: tuple[DeviceMapperRecord, ...]
    containers: tuple[ContainerRecord, ...]
    source_errors: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResidueObservation:
    metric: str
    status: str
    observed_at: str
    residue_detected: bool
    added_qemu_processes: tuple[ProcessRecord, ...]
    added_tun_tap_interfaces: tuple[NetworkInterfaceRecord, ...]
    added_loop_devices: tuple[LoopDeviceRecord, ...]
    added_device_mapper_entries: tuple[DeviceMapperRecord, ...]
    added_containers: tuple[ContainerRecord, ...]
    removed_qemu_processes: tuple[ProcessRecord, ...]
    removed_tun_tap_interfaces: tuple[NetworkInterfaceRecord, ...]
    removed_loop_devices: tuple[LoopDeviceRecord, ...]
    removed_device_mapper_entries: tuple[DeviceMapperRecord, ...]
    removed_containers: tuple[ContainerRecord, ...]
    source_errors: dict[str, str]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


CommandRunner = Callable[
    [Sequence[str]],
    subprocess.CompletedProcess[str],
]


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _default_command_runner(
    command: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def _is_qemu_command(
    executable: str | None,
    command: tuple[str, ...],
) -> bool:
    names: list[str] = []

    if executable:
        names.append(Path(executable).name.lower())

    if command:
        names.append(Path(command[0]).name.lower())

    return any(
        name == "qemu"
        or name.startswith("qemu-")
        or name.startswith("qemu_")
        for name in names
    )


def _read_process_start_ticks(
    stat_text: str,
) -> int:
    """
    Read field 22 from /proc/PID/stat.

    The process name may contain spaces and parentheses, so fields are parsed
    only after the final closing parenthesis.
    """
    closing_parenthesis = stat_text.rfind(")")

    if closing_parenthesis < 0:
        raise ValueError("Malformed /proc stat document")

    remaining_fields = stat_text[
        closing_parenthesis + 2:
    ].split()

    # Field 3 becomes index 0 after removing PID and comm.
    # Therefore field 22 becomes index 19.
    return int(remaining_fields[19])


class HostEnvironmentCollector:
    """
    Collect candidate-neutral host state.

    Collection failures are recorded as evidence rather than silently ignored.
    """

    def __init__(
        self,
        *,
        command_runner: CommandRunner = _default_command_runner,
        proc_root: Path = Path("/proc"),
        sys_class_net: Path = Path("/sys/class/net"),
    ) -> None:
        self.command_runner = command_runner
        self.proc_root = proc_root
        self.sys_class_net = sys_class_net

    def snapshot(self) -> HostEnvironmentSnapshot:
        errors: dict[str, str] = {}

        try:
            qemu_processes = self._collect_qemu_processes()
        except Exception as exc:
            qemu_processes = ()
            errors["qemu_processes"] = (
                f"{type(exc).__name__}: {exc}"
            )

        try:
            tun_tap_interfaces = (
                self._collect_tun_tap_interfaces()
            )
        except Exception as exc:
            tun_tap_interfaces = ()
            errors["tun_tap_interfaces"] = (
                f"{type(exc).__name__}: {exc}"
            )

        try:
            loop_devices = self._collect_loop_devices()
        except Exception as exc:
            loop_devices = ()
            errors["loop_devices"] = (
                f"{type(exc).__name__}: {exc}"
            )

        try:
            device_mapper_entries = (
                self._collect_device_mapper_entries()
            )
        except Exception as exc:
            device_mapper_entries = ()
            errors["device_mapper_entries"] = (
                f"{type(exc).__name__}: {exc}"
            )

        try:
            containers = self._collect_containers()
        except Exception as exc:
            containers = ()
            errors["containers"] = (
                f"{type(exc).__name__}: {exc}"
            )

        return HostEnvironmentSnapshot(
            observed_at=_utc_now(),
            qemu_processes=qemu_processes,
            tun_tap_interfaces=tun_tap_interfaces,
            loop_devices=loop_devices,
            device_mapper_entries=device_mapper_entries,
            containers=containers,
            source_errors=errors,
        )

    def _collect_qemu_processes(
        self,
    ) -> tuple[ProcessRecord, ...]:
        records: list[ProcessRecord] = []

        for pid_directory in sorted(
            self.proc_root.iterdir(),
            key=lambda path: path.name,
        ):
            if not pid_directory.name.isdigit():
                continue

            pid = int(pid_directory.name)

            try:
                stat_text = (
                    pid_directory / "stat"
                ).read_text(
                    encoding="utf-8",
                    errors="replace",
                )

                start_ticks = _read_process_start_ticks(
                    stat_text
                )

                raw_cmdline = (
                    pid_directory / "cmdline"
                ).read_bytes()

                command = tuple(
                    item.decode(
                        "utf-8",
                        errors="replace",
                    )
                    for item in raw_cmdline.split(b"\0")
                    if item
                )

                try:
                    executable = os.readlink(
                        pid_directory / "exe"
                    )
                except OSError:
                    executable = None

            except (
                FileNotFoundError,
                PermissionError,
                ProcessLookupError,
            ):
                continue

            if not _is_qemu_command(
                executable,
                command,
            ):
                continue

            records.append(
                ProcessRecord(
                    pid=pid,
                    start_time_ticks=start_ticks,
                    executable=executable,
                    command=command,
                )
            )

        return tuple(
            sorted(
                records,
                key=lambda record: record.identity,
            )
        )

    def _collect_tun_tap_interfaces(
        self,
    ) -> tuple[NetworkInterfaceRecord, ...]:
        completed = self.command_runner(
            ["ip", "-j", "-d", "link", "show"]
        )

        if completed.returncode != 0:
            raise EnvironmentProbeError(
                "ip link collection failed: "
                f"{completed.stderr.strip()}"
            )

        document = json.loads(completed.stdout)

        if not isinstance(document, list):
            raise EnvironmentProbeError(
                "ip link did not return a JSON array"
            )

        records: list[NetworkInterfaceRecord] = []

        for interface in document:
            if not isinstance(interface, dict):
                continue

            name = interface.get("ifname")

            if not isinstance(name, str):
                continue

            linkinfo = interface.get("linkinfo", {})

            if not isinstance(linkinfo, dict):
                linkinfo = {}

            kind = linkinfo.get("info_kind")

            tun_flags_path = (
                self.sys_class_net
                / name
                / "tun_flags"
            )

            tun_flags: str | None = None

            try:
                tun_flags = tun_flags_path.read_text(
                    encoding="utf-8"
                ).strip()
            except (
                FileNotFoundError,
                PermissionError,
            ):
                pass

            # Linux reports TUN and TAP through the tun driver.
            if kind != "tun" and tun_flags is None:
                continue

            ifindex_raw = interface.get("ifindex")

            records.append(
                NetworkInterfaceRecord(
                    name=name,
                    ifindex=(
                        int(ifindex_raw)
                        if isinstance(ifindex_raw, int)
                        else None
                    ),
                    kind=(
                        str(kind)
                        if kind is not None
                        else None
                    ),
                    tun_flags=tun_flags,
                )
            )

        return tuple(
            sorted(
                records,
                key=lambda record: record.identity,
            )
        )

    def _collect_loop_devices(
        self,
    ) -> tuple[LoopDeviceRecord, ...]:
        completed = self.command_runner(
            [
                "losetup",
                "--json",
                "--list",
                "--output",
                "NAME,BACK-FILE,OFFSET,AUTOCLEAR",
            ]
        )

        if completed.returncode != 0:
            raise EnvironmentProbeError(
                "losetup collection failed: "
                f"{completed.stderr.strip()}"
            )

        document = json.loads(completed.stdout)

        devices = document.get("loopdevices", [])

        if not isinstance(devices, list):
            raise EnvironmentProbeError(
                "losetup JSON contains no loopdevices array"
            )

        records: list[LoopDeviceRecord] = []

        for device in devices:
            if not isinstance(device, dict):
                continue

            name = device.get("name")

            if not isinstance(name, str):
                continue

            offset_raw = device.get("offset")
            autoclear_raw = device.get("autoclear")

            records.append(
                LoopDeviceRecord(
                    name=name,
                    backing_file=(
                        str(device.get("back-file"))
                        if device.get("back-file")
                        else None
                    ),
                    offset_bytes=(
                        int(offset_raw)
                        if offset_raw not in {None, ""}
                        else None
                    ),
                    autoclear=(
                        bool(autoclear_raw)
                        if isinstance(
                            autoclear_raw,
                            bool,
                        )
                        else None
                    ),
                )
            )

        return tuple(
            sorted(
                records,
                key=lambda record: record.identity,
            )
        )

    def _collect_device_mapper_entries(
        self,
    ) -> tuple[DeviceMapperRecord, ...]:
        completed = self.command_runner(
            [
                "dmsetup",
                "info",
                "--columns",
                "--noheadings",
                "--separator",
                "\t",
                "-o",
                "name,uuid,major,minor",
            ]
        )

        if completed.returncode != 0:
            stderr = completed.stderr.strip()

            # An empty device-mapper table may legitimately produce no rows.
            if "No devices found" in stderr:
                return ()

            raise EnvironmentProbeError(
                "dmsetup collection failed: "
                f"{stderr}"
            )

        records: list[DeviceMapperRecord] = []

        for line in completed.stdout.splitlines():
            stripped = line.strip()

            if not stripped:
                continue

            parts = [
                part.strip()
                for part in stripped.split("\t")
            ]

            while len(parts) < 4:
                parts.append("")

            name, uuid, major, minor = parts[:4]

            if not name:
                continue

            records.append(
                DeviceMapperRecord(
                    name=name,
                    uuid=uuid or None,
                    major=(
                        int(major)
                        if major.isdigit()
                        else None
                    ),
                    minor=(
                        int(minor)
                        if minor.isdigit()
                        else None
                    ),
                )
            )

        return tuple(
            sorted(
                records,
                key=lambda record: record.identity,
            )
        )

    def _collect_containers(
        self,
    ) -> tuple[ContainerRecord, ...]:
        completed = self.command_runner(
            [
                "docker",
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ]
        )

        if completed.returncode != 0:
            raise EnvironmentProbeError(
                "Docker container collection failed: "
                f"{completed.stderr.strip()}"
            )

        records: list[ContainerRecord] = []

        for line in completed.stdout.splitlines():
            if not line.strip():
                continue

            summary = json.loads(line)

            container_id = summary.get("ID")

            if not isinstance(container_id, str):
                continue

            inspection = self.command_runner(
                [
                    "docker",
                    "container",
                    "inspect",
                    container_id,
                ]
            )

            if inspection.returncode != 0:
                continue

            documents = json.loads(inspection.stdout)

            if (
                not isinstance(documents, list)
                or not documents
                or not isinstance(documents[0], dict)
            ):
                continue

            detail = documents[0]
            config = detail.get("Config", {})
            state = detail.get("State", {})

            if not isinstance(config, dict):
                config = {}

            if not isinstance(state, dict):
                state = {}

            labels = config.get("Labels")

            if not isinstance(labels, dict):
                labels = {}

            records.append(
                ContainerRecord(
                    container_id=container_id,
                    name=str(
                        detail.get("Name", "")
                    ).lstrip("/"),
                    image=str(
                        config.get("Image", "")
                    ),
                    state=str(
                        state.get("Status", "")
                    ),
                    labels={
                        str(key): str(value)
                        for key, value in labels.items()
                    },
                )
            )

        return tuple(
            sorted(
                records,
                key=lambda record: record.identity,
            )
        )


def _added_records(
    before: Sequence[Any],
    after: Sequence[Any],
) -> tuple[Any, ...]:
    before_identities = {
        record.identity
        for record in before
    }

    return tuple(
        record
        for record in after
        if record.identity not in before_identities
    )


def _removed_records(
    before: Sequence[Any],
    after: Sequence[Any],
) -> tuple[Any, ...]:
    after_identities = {
        record.identity
        for record in after
    }

    return tuple(
        record
        for record in before
        if record.identity not in after_identities
    )


def evaluate_environment_residue(
    before: HostEnvironmentSnapshot,
    after: HostEnvironmentSnapshot,
) -> ResidueObservation:
    added_qemu = _added_records(
        before.qemu_processes,
        after.qemu_processes,
    )
    added_interfaces = _added_records(
        before.tun_tap_interfaces,
        after.tun_tap_interfaces,
    )
    added_loops = _added_records(
        before.loop_devices,
        after.loop_devices,
    )
    added_mapper = _added_records(
        before.device_mapper_entries,
        after.device_mapper_entries,
    )
    added_containers = _added_records(
        before.containers,
        after.containers,
    )

    removed_qemu = _removed_records(
        before.qemu_processes,
        after.qemu_processes,
    )
    removed_interfaces = _removed_records(
        before.tun_tap_interfaces,
        after.tun_tap_interfaces,
    )
    removed_loops = _removed_records(
        before.loop_devices,
        after.loop_devices,
    )
    removed_mapper = _removed_records(
        before.device_mapper_entries,
        after.device_mapper_entries,
    )
    removed_containers = _removed_records(
        before.containers,
        after.containers,
    )

    residue_detected = any(
        (
            added_qemu,
            added_interfaces,
            added_loops,
            added_mapper,
            added_containers,
        )
    )

    source_errors = {
        **{
            f"before.{key}": value
            for key, value in before.source_errors.items()
        },
        **{
            f"after.{key}": value
            for key, value in after.source_errors.items()
        },
    }

    available_sources = 5 - len(
        {
            key.split(".", 1)[-1]
            for key in source_errors
        }
    )

    if residue_detected:
        status = "residue_detected"
        reason = (
            "One or more resources created during the run "
            "remained after candidate shutdown"
        )
    elif available_sources <= 0:
        status = "probe_error"
        reason = (
            "No environmental residue source could be "
            "measured successfully"
        )
    elif source_errors:
        status = "partial_probe"
        reason = (
            "No residue was detected by available checks, but "
            "one or more host-state sources were unavailable"
        )
    else:
        status = "clean"
        reason = (
            "No new QEMU process, TUN/TAP interface, loop "
            "device, device-mapper entry or container remained"
        )

    return ResidueObservation(
        metric="environmental_residue",
        status=status,
        observed_at=_utc_now(),
        residue_detected=residue_detected,
        added_qemu_processes=added_qemu,
        added_tun_tap_interfaces=added_interfaces,
        added_loop_devices=added_loops,
        added_device_mapper_entries=added_mapper,
        added_containers=added_containers,
        removed_qemu_processes=removed_qemu,
        removed_tun_tap_interfaces=removed_interfaces,
        removed_loop_devices=removed_loops,
        removed_device_mapper_entries=removed_mapper,
        removed_containers=removed_containers,
        source_errors=source_errors,
        reason=reason,
    )


def write_environment_evidence(
    *,
    contract_root: Path,
    before: HostEnvironmentSnapshot,
    after: HostEnvironmentSnapshot,
    observation: ResidueObservation,
) -> None:
    artifact_directory = (
        contract_root.resolve() / "artifacts"
    )
    artifact_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    documents = {
        "environment-before.json": before.to_dict(),
        "environment-after.json": after.to_dict(),
        "environment-residue.json": observation.to_dict(),
    }

    for filename, document in documents.items():
        output_path = artifact_directory / filename
        temporary_path = output_path.with_suffix(".tmp")

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
