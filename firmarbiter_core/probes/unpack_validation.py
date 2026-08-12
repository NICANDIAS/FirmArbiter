from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


class UnpackValidationError(RuntimeError):
    """Raised when independent unpack validation cannot be configured."""


@dataclass(frozen=True)
class UnpackObservation:
    metric: str
    status: str
    observed_at: str
    export_path: str
    entry_count: int
    regular_files: int
    directories: int
    symlinks: int
    special_files: int
    total_regular_bytes: int
    elf_files: int
    shebang_scripts: int
    anchor_groups_detected: tuple[str, ...]
    required_anchor_groups: int
    tree_sha256: str | None
    inventory_complete: bool
    errors: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["anchor_groups_detected"] = list(
            self.anchor_groups_detected
        )
        document["errors"] = list(self.errors)
        return document


@dataclass(frozen=True)
class _InventoryEntry:
    relative_path: str
    entry_type: str
    mode: int
    size_bytes: int | None
    content_sha256: str | None
    symlink_target: str | None


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _normalise_relative_path(
    path: Path,
    root: Path,
) -> str:
    relative = path.relative_to(root)
    return PurePosixPath(*relative.parts).as_posix()


def _read_regular_file(
    path: Path,
) -> tuple[str, int, bool, bool]:
    digest = hashlib.sha256()
    size = 0
    prefix = b""

    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)

            if not block:
                break

            if len(prefix) < 4:
                prefix += block[: 4 - len(prefix)]

            digest.update(block)
            size += len(block)

    return (
        digest.hexdigest(),
        size,
        prefix.startswith(b"\x7fELF"),
        prefix.startswith(b"#!"),
    )


def _tree_digest(
    entries: list[_InventoryEntry],
) -> str:
    digest = hashlib.sha256()

    for entry in sorted(
        entries,
        key=lambda item: item.relative_path,
    ):
        document = {
            "path": entry.relative_path,
            "type": entry.entry_type,
            "mode": entry.mode,
            "size_bytes": entry.size_bytes,
            "content_sha256": entry.content_sha256,
            "symlink_target": entry.symlink_target,
        }

        digest.update(
            json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")

    return digest.hexdigest()


def _detect_anchor_groups(
    paths: set[str],
) -> tuple[str, ...]:
    groups: dict[str, set[str]] = {
        "initialisation": {
            "init",
            "bin/init",
            "sbin/init",
            "etc/inittab",
            "etc/init.d",
        },
        "shell": {
            "bin/sh",
            "bin/ash",
            "bin/bash",
            "bin/busybox",
        },
        "configuration": {
            "etc/passwd",
            "etc/group",
            "etc/os-release",
        },
        "libraries": {
            "lib",
            "lib64",
            "usr/lib",
        },
        "executable_tree": {
            "bin",
            "sbin",
            "usr/bin",
            "usr/sbin",
        },
    }

    detected = [
        group_name
        for group_name, candidates in groups.items()
        if candidates.intersection(paths)
    ]

    return tuple(sorted(detected))


def validate_unpack_export(
    *,
    contract_root: Path,
    requested: bool,
    required_anchor_groups: int = 3,
    max_entries: int = 500000,
) -> UnpackObservation:
    """
    Independently validate an adapter-exported Linux root filesystem.

    Symlinks are inventoried but never followed.
    """
    if required_anchor_groups < 1:
        raise UnpackValidationError(
            "required_anchor_groups must be positive"
        )

    if max_entries < 1:
        raise UnpackValidationError(
            "max_entries must be positive"
        )

    rootfs = (
        contract_root.resolve()
        / "artifacts"
        / "unpack"
        / "rootfs"
    )

    if not requested:
        return UnpackObservation(
            metric="unpack_success",
            status="not_attempted",
            observed_at=_utc_now(),
            export_path=str(rootfs),
            entry_count=0,
            regular_files=0,
            directories=0,
            symlinks=0,
            special_files=0,
            total_regular_bytes=0,
            elf_files=0,
            shebang_scripts=0,
            anchor_groups_detected=(),
            required_anchor_groups=required_anchor_groups,
            tree_sha256=None,
            inventory_complete=False,
            errors=(),
            reason=(
                "The experiment did not request the unpack stage"
            ),
        )

    if not rootfs.exists():
        return UnpackObservation(
            metric="unpack_success",
            status="false",
            observed_at=_utc_now(),
            export_path=str(rootfs),
            entry_count=0,
            regular_files=0,
            directories=0,
            symlinks=0,
            special_files=0,
            total_regular_bytes=0,
            elf_files=0,
            shebang_scripts=0,
            anchor_groups_detected=(),
            required_anchor_groups=required_anchor_groups,
            tree_sha256=None,
            inventory_complete=True,
            errors=(),
            reason=(
                "Unpack was requested, but the adapter exported no "
                "root filesystem"
            ),
        )

    if not rootfs.is_dir():
        return UnpackObservation(
            metric="unpack_success",
            status="false",
            observed_at=_utc_now(),
            export_path=str(rootfs),
            entry_count=0,
            regular_files=0,
            directories=0,
            symlinks=0,
            special_files=0,
            total_regular_bytes=0,
            elf_files=0,
            shebang_scripts=0,
            anchor_groups_detected=(),
            required_anchor_groups=required_anchor_groups,
            tree_sha256=None,
            inventory_complete=True,
            errors=(),
            reason=(
                "The adapter unpack export exists but is not a directory"
            ),
        )

    inventory: list[_InventoryEntry] = []
    discovered_paths: set[str] = set()
    errors: list[str] = []

    regular_files = 0
    directories = 0
    symlinks = 0
    special_files = 0
    total_regular_bytes = 0
    elf_files = 0
    shebang_scripts = 0

    pending_directories: list[Path] = [rootfs]

    try:
        while pending_directories:
            current_directory = pending_directories.pop()

            try:
                directory_entries = sorted(
                    os.scandir(current_directory),
                    key=lambda entry: entry.name,
                )
            except OSError as exc:
                errors.append(
                    f"{current_directory}: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

            for directory_entry in directory_entries:
                if len(inventory) >= max_entries:
                    errors.append(
                        "Maximum independent inventory entry limit "
                        f"reached: {max_entries}"
                    )
                    pending_directories.clear()
                    break

                path = Path(directory_entry.path)
                relative_path = _normalise_relative_path(
                    path,
                    rootfs,
                )

                discovered_paths.add(relative_path)

                try:
                    metadata = directory_entry.stat(
                        follow_symlinks=False
                    )
                except OSError as exc:
                    errors.append(
                        f"{relative_path}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue

                permission_mode = stat.S_IMODE(
                    metadata.st_mode
                )

                if stat.S_ISDIR(metadata.st_mode):
                    directories += 1

                    inventory.append(
                        _InventoryEntry(
                            relative_path=relative_path,
                            entry_type="directory",
                            mode=permission_mode,
                            size_bytes=None,
                            content_sha256=None,
                            symlink_target=None,
                        )
                    )

                    pending_directories.append(path)

                elif stat.S_ISREG(metadata.st_mode):
                    regular_files += 1

                    try:
                        (
                            content_hash,
                            size_bytes,
                            is_elf,
                            is_shebang,
                        ) = _read_regular_file(path)

                        total_regular_bytes += size_bytes
                        elf_files += int(is_elf)
                        shebang_scripts += int(
                            is_shebang
                        )

                        inventory.append(
                            _InventoryEntry(
                                relative_path=relative_path,
                                entry_type="regular_file",
                                mode=permission_mode,
                                size_bytes=size_bytes,
                                content_sha256=content_hash,
                                symlink_target=None,
                            )
                        )

                    except OSError as exc:
                        errors.append(
                            f"{relative_path}: "
                            f"{type(exc).__name__}: {exc}"
                        )

                elif stat.S_ISLNK(metadata.st_mode):
                    symlinks += 1

                    try:
                        target = os.readlink(path)
                    except OSError as exc:
                        target = None
                        errors.append(
                            f"{relative_path}: "
                            f"{type(exc).__name__}: {exc}"
                        )

                    inventory.append(
                        _InventoryEntry(
                            relative_path=relative_path,
                            entry_type="symlink",
                            mode=permission_mode,
                            size_bytes=None,
                            content_sha256=None,
                            symlink_target=target,
                        )
                    )

                else:
                    special_files += 1

                    inventory.append(
                        _InventoryEntry(
                            relative_path=relative_path,
                            entry_type="special",
                            mode=permission_mode,
                            size_bytes=None,
                            content_sha256=None,
                            symlink_target=None,
                        )
                    )

    except Exception as exc:
        return UnpackObservation(
            metric="unpack_success",
            status="probe_error",
            observed_at=_utc_now(),
            export_path=str(rootfs),
            entry_count=len(inventory),
            regular_files=regular_files,
            directories=directories,
            symlinks=symlinks,
            special_files=special_files,
            total_regular_bytes=total_regular_bytes,
            elf_files=elf_files,
            shebang_scripts=shebang_scripts,
            anchor_groups_detected=(),
            required_anchor_groups=required_anchor_groups,
            tree_sha256=None,
            inventory_complete=False,
            errors=(
                f"{type(exc).__name__}: {exc}",
            ),
            reason=(
                "FIRMARBITER encountered an unexpected error while "
                "inventorying the unpack export"
            ),
        )

    anchor_groups = _detect_anchor_groups(
        discovered_paths
    )

    tree_hash = (
        _tree_digest(inventory)
        if inventory
        else hashlib.sha256(b"").hexdigest()
    )

    executable_evidence = (
        elf_files > 0
        or shebang_scripts > 0
    )

    sufficient_anchors = (
        len(anchor_groups)
        >= required_anchor_groups
    )

    inventory_complete = not errors

    if errors:
        status = "inconclusive"
        reason = (
            "An unpack export was found, but FIRMARBITER could not "
            "inventory it completely"
        )

    elif (
        regular_files > 0
        and executable_evidence
        and sufficient_anchors
    ):
        status = "true"
        reason = (
            "The exported tree contains regular files, executable "
            "evidence and sufficient independent Linux root-filesystem "
            "anchors"
        )

    else:
        status = "false"

        failed_conditions: list[str] = []

        if regular_files == 0:
            failed_conditions.append(
                "no regular files"
            )

        if not executable_evidence:
            failed_conditions.append(
                "no ELF file or shebang script"
            )

        if not sufficient_anchors:
            failed_conditions.append(
                "insufficient Linux root-filesystem anchors"
            )

        reason = (
            "The exported tree failed independent validation: "
            + ", ".join(failed_conditions)
        )

    return UnpackObservation(
        metric="unpack_success",
        status=status,
        observed_at=_utc_now(),
        export_path=str(rootfs),
        entry_count=len(inventory),
        regular_files=regular_files,
        directories=directories,
        symlinks=symlinks,
        special_files=special_files,
        total_regular_bytes=total_regular_bytes,
        elf_files=elf_files,
        shebang_scripts=shebang_scripts,
        anchor_groups_detected=anchor_groups,
        required_anchor_groups=required_anchor_groups,
        tree_sha256=tree_hash,
        inventory_complete=inventory_complete,
        errors=tuple(errors),
        reason=reason,
    )


def write_unpack_evidence(
    *,
    contract_root: Path,
    observation: UnpackObservation,
) -> None:
    output_path = (
        contract_root.resolve()
        / "artifacts"
        / "unpack-observation.json"
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = output_path.with_suffix(
        ".tmp"
    )

    temporary_path.write_text(
        json.dumps(
            observation.to_dict(),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    temporary_path.replace(output_path)
