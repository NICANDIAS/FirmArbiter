#!/usr/bin/env python3
"""
tools/validate_events.py — standalone event-stream validator.

Usage:
    python tools/validate_events.py path/to/events.jsonl
    python tools/validate_events.py path/to/events.jsonl --schema path/to/schema.json

Validates every line of a JSONL events file against the adapter event
schema and reports violations with line numbers, so an adapter author
can catch contract violations locally before ever running the
coordinator. This is the check that would have caught FIRMADYNE's
extra-field and missing-message issues in seconds instead of at the
end of a long run.
"""

import argparse
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

DEFAULT_SCHEMA_PATH = "schemas/adapter-event-v1.schema.json"

ALLOWED_FIELDS = {
    "schema_version", "contract_version", "event", "sequence", "timestamp",
    "run_id", "adapter_id", "state", "endpoint", "error", "outcome",
    "stage", "stage_outcome", "message",
}


def validate_file(events_path, schema_path):
    schema = json.loads(Path(schema_path).read_text())
    validator = Draft202012Validator(schema)

    total = 0
    violations = 0
    last_sequence = None

    with open(events_path) as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            total += 1

            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"Line {line_number}: INVALID JSON — {e}")
                violations += 1
                continue

            unknown = set(event.keys()) - ALLOWED_FIELDS
            if unknown:
                print(f"Line {line_number}: fields not permitted by contract: {sorted(unknown)}")
                violations += 1

            errors = sorted(validator.iter_errors(event), key=lambda e: e.path)
            for e in errors:
                print(f"Line {line_number}: {list(e.path)}: {e.message}")
                violations += 1

            # Sequence monotonicity check — not schema-enforced, but a real
            # ordering bug is worth flagging locally too.
            seq = event.get("sequence")
            if seq is not None and last_sequence is not None and seq <= last_sequence:
                print(f"Line {line_number}: sequence {seq} is not greater than previous sequence {last_sequence}")
                violations += 1
            if seq is not None:
                last_sequence = seq

    return total, violations


def main():
    parser = argparse.ArgumentParser(description="Validate a VERITAS adapter events.jsonl file")
    parser.add_argument("events_file", help="Path to events.jsonl")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA_PATH, help="Path to adapter-event-v1.schema.json")
    args = parser.parse_args()

    if not Path(args.events_file).exists():
        print(f"ERROR: events file not found: {args.events_file}", file=sys.stderr)
        sys.exit(2)
    if not Path(args.schema).exists():
        print(f"ERROR: schema file not found: {args.schema}", file=sys.stderr)
        sys.exit(2)

    total, violations = validate_file(args.events_file, args.schema)

    print()
    print(f"Checked {total} events, {violations} violation(s) found.")
    sys.exit(1 if violations else 0)


if __name__ == "__main__":
    main()
