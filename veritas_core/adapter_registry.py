from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


class AdapterRegistryError(RuntimeError):
    """Raised when adapter discovery or validation fails."""


@dataclass(frozen=True)
class AdapterRecord:
    """A validated adapter discovered by the neutral core."""

    adapter_id: str
    package_dir: Path
    manifest_path: Path
    manifest_sha256: str
    manifest: dict[str, Any]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file_handle:
        for block in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def _require_path_within(
    parent: Path,
    child: Path,
    description: str,
) -> Path:
    """
    Resolve a package-controlled path and reject directory traversal.

    This prevents an adapter manifest from referring to files outside its
    adapter package.
    """
    resolved_parent = parent.resolve()
    resolved_child = child.resolve()

    try:
        resolved_child.relative_to(resolved_parent)
    except ValueError as exc:
        raise AdapterRegistryError(
            f"{description} escapes adapter package: {child}"
        ) from exc

    return resolved_child


def _load_schema(schema_path: Path) -> dict[str, Any]:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AdapterRegistryError(
            f"Adapter manifest schema not found: {schema_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AdapterRegistryError(
            f"Invalid JSON schema {schema_path}: {exc}"
        ) from exc

    Draft202012Validator.check_schema(schema)
    return schema


def _load_yaml_mapping(manifest_path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(
            manifest_path.read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise AdapterRegistryError(
            f"Adapter manifest not found: {manifest_path}"
        ) from exc
    except yaml.YAMLError as exc:
        raise AdapterRegistryError(
            f"Invalid YAML in {manifest_path}: {exc}"
        ) from exc

    if not isinstance(document, dict):
        raise AdapterRegistryError(
            f"Adapter manifest must contain a YAML mapping: {manifest_path}"
        )

    return document


def _validate_schema(
    manifest: dict[str, Any],
    schema: dict[str, Any],
    manifest_path: Path,
) -> None:
    validator = Draft202012Validator(schema)

    errors = sorted(
        validator.iter_errors(manifest),
        key=lambda error: list(error.absolute_path),
    )

    if not errors:
        return

    messages: list[str] = []

    for error in errors:
        location = ".".join(
            str(part) for part in error.absolute_path
        )
        messages.append(
            f"{location or '<root>'}: {error.message}"
        )

    joined = "\n  - ".join(messages)

    raise AdapterRegistryError(
        f"Manifest validation failed for {manifest_path}:\n"
        f"  - {joined}"
    )


def _validate_package_paths(
    package_dir: Path,
    manifest: dict[str, Any],
) -> None:
    build = manifest["build"]

    context_path = _require_path_within(
        package_dir,
        package_dir / build["context"],
        "Docker build context",
    )

    if not context_path.is_dir():
        raise AdapterRegistryError(
            f"Docker build context does not exist: {context_path}"
        )

    dockerfile_path = _require_path_within(
        context_path,
        context_path / build["dockerfile"],
        "Dockerfile",
    )

    if not dockerfile_path.is_file():
        raise AdapterRegistryError(
            f"Dockerfile does not exist: {dockerfile_path}"
        )

    for patch in manifest["candidate"].get("patches", []):
        patch_path = _require_path_within(
            package_dir,
            package_dir / patch["path"],
            "Compatibility patch",
        )

        if not patch_path.is_file():
            raise AdapterRegistryError(
                f"Compatibility patch does not exist: {patch_path}"
            )

        actual_hash = _sha256_file(patch_path)

        if actual_hash != patch["sha256"]:
            raise AdapterRegistryError(
                f"Patch hash mismatch for {patch_path}: "
                f"declared={patch['sha256']} actual={actual_hash}"
            )


def load_adapter(
    manifest_path: Path,
    schema_path: Path,
) -> AdapterRecord:
    """
    Load and validate one adapter package.

    This function contains no knowledge of any candidate name.
    """
    manifest_path = manifest_path.resolve()
    package_dir = manifest_path.parent

    schema = _load_schema(schema_path.resolve())
    manifest = _load_yaml_mapping(manifest_path)

    _validate_schema(manifest, schema, manifest_path)
    _validate_package_paths(package_dir, manifest)

    adapter_id = manifest["adapter"]["id"]

    return AdapterRecord(
        adapter_id=adapter_id,
        package_dir=package_dir,
        manifest_path=manifest_path,
        manifest_sha256=_sha256_file(manifest_path),
        manifest=manifest,
    )


def discover_adapters(
    adapters_root: Path,
    schema_path: Path,
) -> dict[str, AdapterRecord]:
    """
    Discover every immediate child containing adapter.yaml.

    Adapter identifiers are read from validated manifests. No built-in list of
    supported candidates exists.
    """
    adapters_root = adapters_root.resolve()

    if not adapters_root.is_dir():
        raise AdapterRegistryError(
            f"Adapter directory does not exist: {adapters_root}"
        )

    discovered: dict[str, AdapterRecord] = {}

    for manifest_path in sorted(adapters_root.glob("*/adapter.yaml")):
        record = load_adapter(manifest_path, schema_path)

        if record.adapter_id in discovered:
            first = discovered[record.adapter_id].manifest_path

            raise AdapterRegistryError(
                f"Duplicate adapter id '{record.adapter_id}': "
                f"{first} and {record.manifest_path}"
            )

        discovered[record.adapter_id] = record

    return discovered
