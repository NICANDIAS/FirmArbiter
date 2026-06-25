from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class EventProtocolError(RuntimeError):
    """Raised when an adapter emits an invalid contract event."""


def _format_error_location(error: Any) -> str:
    location = ".".join(
        str(part) for part in error.absolute_path
    )
    return location or "<root>"


class AdapterEventStream:
    """
    Incrementally reads and validates an adapter JSONL event stream.

    Candidate stdout and stderr are deliberately not interpreted here.
    """

    def __init__(
        self,
        event_path: Path,
        schema_path: Path,
        expected_run_id: str,
        expected_adapter_id: str,
    ) -> None:
        self.event_path = event_path
        self.expected_run_id = expected_run_id
        self.expected_adapter_id = expected_adapter_id

        try:
            schema = json.loads(
                schema_path.read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise EventProtocolError(
                f"Event schema not found: {schema_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise EventProtocolError(
                f"Invalid event schema JSON: {exc}"
            ) from exc

        Draft202012Validator.check_schema(schema)

        self._validator = Draft202012Validator(
            schema,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )

        self._offset = 0
        self._next_sequence = 1
        self._adapter_started = False
        self._adapter_stopped = False

    def _validate_document(
        self,
        document: dict[str, Any],
        line_number: int,
    ) -> None:
        errors = sorted(
            self._validator.iter_errors(document),
            key=lambda error: list(error.absolute_path),
        )

        if errors:
            messages = "; ".join(
                f"{_format_error_location(error)}: {error.message}"
                for error in errors
            )
            raise EventProtocolError(
                f"Invalid event at JSONL line {line_number}: "
                f"{messages}"
            )

        if document["run_id"] != self.expected_run_id:
            raise EventProtocolError(
                f"Event run_id mismatch: expected "
                f"{self.expected_run_id!r}, received "
                f"{document['run_id']!r}"
            )

        if document["adapter_id"] != self.expected_adapter_id:
            raise EventProtocolError(
                f"Event adapter_id mismatch: expected "
                f"{self.expected_adapter_id!r}, received "
                f"{document['adapter_id']!r}"
            )

        if document["sequence"] != self._next_sequence:
            raise EventProtocolError(
                f"Invalid event sequence: expected "
                f"{self._next_sequence}, received "
                f"{document['sequence']}"
            )

        event_name = document["event"]

        if self._adapter_stopped:
            raise EventProtocolError(
                "Adapter emitted an event after adapter_stopped"
            )

        if not self._adapter_started:
            if event_name != "adapter_started":
                raise EventProtocolError(
                    "The first event must be adapter_started"
                )
            self._adapter_started = True

        elif event_name == "adapter_started":
            raise EventProtocolError(
                "adapter_started may be emitted only once"
            )

        event_specific_fields = {
            "endpoint": "endpoint_reported",
            "error": "error",
            "outcome": "adapter_stopped",
        }

        for field_name, permitted_event in event_specific_fields.items():
            if (
                field_name in document
                and event_name != permitted_event
            ):
                raise EventProtocolError(
                    f"Field {field_name!r} is valid only for "
                    f"{permitted_event!r}"
                )

        if event_name == "adapter_stopped":
            self._adapter_stopped = True

        self._next_sequence += 1

    def read_new(self) -> list[dict[str, Any]]:
        """
        Read all complete new JSONL records.

        A partially written final line is left unread until it is completed.
        """
        if not self.event_path.exists():
            return []

        records: list[dict[str, Any]] = []

        with self.event_path.open("rb") as handle:
            handle.seek(self._offset)

            while True:
                line_start = handle.tell()
                raw_line = handle.readline()

                if not raw_line:
                    break

                if not raw_line.endswith(b"\n"):
                    handle.seek(line_start)
                    break

                self._offset = handle.tell()
                line_number = self._next_sequence

                try:
                    decoded = raw_line.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise EventProtocolError(
                        f"Event line is not valid UTF-8: {exc}"
                    ) from exc

                try:
                    document = json.loads(decoded)
                except json.JSONDecodeError as exc:
                    raise EventProtocolError(
                        f"Invalid JSON at event line "
                        f"{line_number}: {exc}"
                    ) from exc

                if not isinstance(document, dict):
                    raise EventProtocolError(
                        f"Event line {line_number} must contain "
                        f"a JSON object"
                    )

                self._validate_document(
                    document,
                    line_number,
                )
                records.append(document)

        return records
