# adapters/_template/lifecycle/request.py
"""
load_and_validate_request() — reads /firmarbiter/input/request.json and returns
a validated, attribute-accessible view of it. Generalized so it is not
shaped around any one candidate tool's assumptions.
"""

import json
from pathlib import Path
from types import SimpleNamespace


class RequestContractError(Exception):
    """Raised when request.json is missing required structure."""
    pass


REQUIRED_TOP_LEVEL = {"schema_version", "contract_version", "run", "firmware", "lifecycle", "paths"}
REQUIRED_RUN_FIELDS = {"run_id", "adapter_id", "requested_stages"}
REQUIRED_FIRMWARE_FIELDS = {"path", "sha256", "size_bytes"}
REQUIRED_LIFECYCLE_FIELDS = {
    "heartbeat_interval_seconds", "heartbeat_timeout_seconds",
    "boot_wait_timeout_seconds", "endpoint_wait_timeout_seconds",
    "shutdown_grace_seconds",
}
REQUIRED_PATHS_FIELDS = {"events", "artifacts", "control", "workspace"}


def _check_fields(section_name, section_dict, required_fields):
    missing = required_fields - set(section_dict.keys())
    if missing:
        raise RequestContractError(
            f"request.json '{section_name}' section is missing required "
            f"fields: {sorted(missing)}"
        )


def _to_namespace(d):
    """Recursively convert nested dicts to SimpleNamespace for dot access,
    while leaving the raw dict available via ._raw for anything unusual."""
    if isinstance(d, dict):
        ns = SimpleNamespace(**{k: _to_namespace(v) for k, v in d.items()})
        ns._raw = d
        return ns
    if isinstance(d, list):
        return [_to_namespace(item) for item in d]
    return d


def load_and_validate_request(request_path="/firmarbiter/input/request.json"):
    path = Path(request_path)
    if not path.exists():
        raise RequestContractError(f"request.json not found at {request_path}")

    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise RequestContractError(f"request.json is not valid JSON: {e}")

    _check_fields("top-level", raw, REQUIRED_TOP_LEVEL)
    _check_fields("run", raw["run"], REQUIRED_RUN_FIELDS)
    _check_fields("firmware", raw["firmware"], REQUIRED_FIRMWARE_FIELDS)
    _check_fields("lifecycle", raw["lifecycle"], REQUIRED_LIFECYCLE_FIELDS)
    _check_fields("paths", raw["paths"], REQUIRED_PATHS_FIELDS)

    if not isinstance(raw["run"]["requested_stages"], list) or not raw["run"]["requested_stages"]:
        raise RequestContractError(
            "request.json 'run.requested_stages' must be a non-empty list"
        )

    return _to_namespace(raw)
