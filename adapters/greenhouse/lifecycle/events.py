# adapters/_template/lifecycle/events.py
"""
EventWriter — the single point through which an adapter emits contract events.

Design goal: it must be structurally impossible to write a malformed event.
Every event is validated against schemas/adapter-event-v1.schema.json before
a single byte touches disk. This directly targets the failure class hit
during FIRMADYNE onboarding (extra fields, missing required fields, bad
enum values all silently killing the coordinator run).
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

# Fields the schema permits. Kept here too so a bad field is caught
# at construction time with a clear message, not just a jsonschema trace.
ALLOWED_FIELDS = {
    "schema_version", "contract_version", "event", "sequence", "timestamp",
    "run_id", "adapter_id", "state", "endpoint", "error", "outcome",
    "stage", "stage_outcome", "message",
}

VALID_ERROR_PHASES = {
    "adapter_setup", "candidate_setup", "candidate_execution",
    "shutdown", "cleanup", "contract",
}


class EventContractError(Exception):
    """Raised when an event fails local validation before it would be written."""
    pass


class EventWriter:
    def __init__(self, events_path, schema_path, run_id, adapter_id,
                 schema_version="1.0", contract_version="1.0"):
        self.events_path = Path(events_path)
        self.run_id = run_id
        self.adapter_id = adapter_id
        self.schema_version = schema_version
        self.contract_version = contract_version

        schema = json.loads(Path(schema_path).read_text())
        self._validator = Draft202012Validator(schema)

        self._lock = threading.Lock()
        self._sequence = 0

        # Fail fast if the output directory doesn't exist — better here
        # than a silent write failure mid-run.
        self.events_path.parent.mkdir(parents=True, exist_ok=True)

    def _next_sequence(self):
        self._sequence += 1
        return self._sequence

    def _timestamp(self):
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _base_event(self, event_name):
        return {
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "event": event_name,
            "sequence": self._next_sequence(),
            "timestamp": self._timestamp(),
            "run_id": self.run_id,
            "adapter_id": self.adapter_id,
        }

    def _validate_and_write(self, event):
        # 1. Reject unknown fields before even asking jsonschema — gives a
        #    much more actionable error than a generic additionalProperties trace.
        unknown = set(event.keys()) - ALLOWED_FIELDS
        if unknown:
            raise EventContractError(
                f"Event '{event.get('event')}' has fields not permitted by the "
                f"contract: {sorted(unknown)}. Allowed fields: {sorted(ALLOWED_FIELDS)}"
            )

        # 2. Full schema validation (required fields, enum values, nested
        #    structures like endpoint, etc.)
        errors = sorted(self._validator.iter_errors(event), key=lambda e: e.path)
        if errors:
            messages = "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
            raise EventContractError(
                f"Event '{event.get('event')}' failed schema validation: {messages}"
            )

        # 3. Write only after validation passes. One JSON object per line.
        with self._lock:
            with open(self.events_path, "a") as f:
                f.write(json.dumps(event) + "\n")

    # --- Public event emitters -------------------------------------------

    def adapter_started(self, message=None):
        event = self._base_event("adapter_started")
        if message:
            event["message"] = message
        self._validate_and_write(event)

    def stage_completed(self, stage, stage_outcome, message):
        # message is required by the schema for this event type — enforce
        # it here too so the failure surfaces at the call site, not deep
        # inside jsonschema.
        if not message:
            raise EventContractError(
                "stage_completed requires a non-empty 'message' field."
            )
        event = self._base_event("stage_completed")
        event.update({
            "stage": stage,
            "stage_outcome": stage_outcome,
            "message": message,
        })
        self._validate_and_write(event)

    def endpoint_reported(self, protocol, host, port):
        event = self._base_event("endpoint_reported")
        event["endpoint"] = {"protocol": protocol, "host": host, "port": port}
        self._validate_and_write(event)

    def error(self, code, phase, message, recoverable, outcome=None):
        """
        code:        short machine-readable identifier, e.g. "STAGE_NOT_IMPLEMENTED".
                     Must match ^[A-Z][A-Z0-9_]{2,63}$ (schema-enforced).
        phase:       one of VALID_ERROR_PHASES
        message:     human-readable detail
        recoverable: whether the adapter can continue after this error,
                     or whether it's fatal to the run — required by schema,
                     not optional, so the caller must make this judgement
                     explicitly rather than it defaulting silently.
        """
        if phase not in VALID_ERROR_PHASES:
            raise EventContractError(
                f"Invalid error phase '{phase}'. Must be one of: {sorted(VALID_ERROR_PHASES)}"
            )
        if not isinstance(recoverable, bool):
            raise EventContractError(
                f"'recoverable' must be a boolean, got {type(recoverable).__name__}"
            )
        event = self._base_event("error")
        event["error"] = {
            "code": code,
            "phase": phase,
            "message": message,
            "recoverable": recoverable,
        }
        if outcome:
            event["outcome"] = outcome
        self._validate_and_write(event)

    def shutdown_started(self, message=None):
        event = self._base_event("shutdown_started")
        if message:
            event["message"] = message
        self._validate_and_write(event)

    def cleanup_complete(self, message=None):
        event = self._base_event("cleanup_complete")
        if message:
            event["message"] = message
        self._validate_and_write(event)

    def adapter_stopped(self, outcome=None, message=None):
        event = self._base_event("adapter_stopped")
        if outcome:
            event["outcome"] = outcome
        if message:
            event["message"] = message
        self._validate_and_write(event)

    def heartbeat(self, state=None):
        event = self._base_event("heartbeat")
        if state:
            event["state"] = state
        self._validate_and_write(event)
