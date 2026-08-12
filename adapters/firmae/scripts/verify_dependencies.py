#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REQUIRED_COMMANDS = (
    "binwalk",
    "file",
    "pg_ctlcluster",
    "psql",
    "qemu-img",
    "qemu-system-arm",
    "qemu-system-mips",
    "ubireader_extract_files",
    "unstuff",
)

REQUIRED_FILES = (
    "/opt/firmae/database/schema",
    "/opt/firmae/scripts/util.py",
    "/opt/firmae/sources/extractor/extractor.py",
)

REQUIRED_IMPORTS = (
    "lzo",
    "magic",
    "psycopg2",
    "ubireader",
)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def inspect_dependencies() -> dict[str, Any]:
    commands = {
        command: shutil.which(command)
        for command in REQUIRED_COMMANDS
    }

    files = {
        path: Path(path).is_file()
        for path in REQUIRED_FILES
    }

    imports: dict[str, dict[str, Any]] = {}

    for module_name in REQUIRED_IMPORTS:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            imports[module_name] = {
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            imports[module_name] = {
                "available": True,
                "path": getattr(module, "__file__", None),
            }

    missing_commands = sorted(
        command
        for command, path in commands.items()
        if not path
    )
    missing_files = sorted(
        path
        for path, available in files.items()
        if not available
    )
    missing_imports = sorted(
        module_name
        for module_name, result in imports.items()
        if not result["available"]
    )

    return {
        "schema_version": "1.0",
        "adapter_id": "firmae",
        "observed_at": utc_now(),
        "status": (
            "pass"
            if not (
                missing_commands
                or missing_files
                or missing_imports
            )
            else "fail"
        ),
        "commands": commands,
        "files": files,
        "python_imports": imports,
        "missing_commands": missing_commands,
        "missing_files": missing_files,
        "missing_python_imports": missing_imports,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = inspect_dependencies()
    document = json.dumps(
        report,
        indent=2,
        sort_keys=True,
    ) + "\n"

    if args.output is not None:
        args.output.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        args.output.write_text(
            document,
            encoding="utf-8",
        )
    else:
        sys.stdout.write(document)

    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
