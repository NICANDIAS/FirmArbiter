# adapters/_template/lifecycle/request.py
"""
load_and_validate_request() — reads /firmarbiter/input/request.json and returns
a validated, attribute-accessible view of it.

This validates against the REAL contract schema
(schemas/run-request-v1.schema.json, baked into the image at build time —
see the Dockerfile), rather than a hand-written list of required fields.

Why: an earlier version of this file maintained its own, separate idea of
what a valid request looks like (REQUIRED_TOP_LEVEL, REQUIRED_LIFECYCLE_FIELDS,
etc). It drifted from the real schema — it was missing fields the schema
requires (resources, runtime_grants, integrity) and wrongly required two
fields the schema treats as optional (boot_wait_timeout_seconds,
endpoint_wait_timeout_seconds). That meant a request could be "valid" by
this file's own rules while being invalid by the contract's real rules, or
vice versa. Validating directly against the schema file makes that class of
drift impossible: there is now exactly one definition of a valid request,
and this file just applies it.
"""

import json
from pathlib import Path
from types import SimpleNamespace

from jsonschema import Draft202012Validator

# Baked into the image at build time — see Dockerfile's
# COPY schemas/run-request-v1.schema.json line.
REQUEST_SCHEMA_PATH = "/firmarbiter_adapter/schemas/run-request-v1.schema.json"


class RequestContractError(Exception):
    """Raised when request.json fails schema validation or is unreadable."""
    pass


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


def load_and_validate_request(
    request_path="/firmarbiter/input/request.json",
    schema_path=REQUEST_SCHEMA_PATH,
):
    path = Path(request_path)
    if not path.exists():
        raise RequestContractError(f"request.json not found at {request_path}")

    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise RequestContractError(f"request.json is not valid JSON: {e}")

    try:
        schema = json.loads(Path(schema_path).read_text())
    except FileNotFoundError:
        raise RequestContractError(
            f"run-request-v1.schema.json not found at {schema_path} — "
            f"was it baked into the image? See the Dockerfile's schemas/ "
            f"COPY line."
        )
    except json.JSONDecodeError as e:
        raise RequestContractError(f"run-request-v1.schema.json is not valid JSON: {e}")

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.absolute_path))
    if errors:
        messages = "; ".join(
            f"{'.'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in errors
        )
        raise RequestContractError(f"request.json failed schema validation: {messages}")

    return _to_namespace(raw)
