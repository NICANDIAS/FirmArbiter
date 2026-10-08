"""Single source of truth for FirmArbiter run identities.

A run's id is also its result-folder name and is validated against the
run_id pattern in the request and event schemas:

    ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$      (at most 128 characters)

Before this module the id was built in two places (run_coordinator.py and
run_firmarbiter.py) as <experiment>.<case>.<adapter>.attempt-<n>. With long
case names plus a long adapter id such as "fact-extractor", the id exceeded
128 characters and the run was rejected before the tool ever started (11 of
220 runs in corpus-50-v8). That made whether a tool ran depend on how long
its name was.

Ids that already fit are returned exactly as before, so existing result
folders and "already exists" checks are unaffected. Only an id that would be
too long is shortened: the readable case name is truncated and a hash of the
FULL case name is appended, so shortened ids stay unique and reproducible.
"""

import hashlib

# Must match the {0,127} in the schemas' run_id pattern (1 + 127 characters).
MAX_RUN_ID_LENGTH = 128

_DIGEST_LENGTH = 10
_MIN_CASE_PREFIX = 8


def build_run_id(safe_experiment, safe_case, adapter_id, attempt):
    suffix = f".{adapter_id}.attempt-{attempt}"
    full = f"{safe_experiment}.{safe_case}{suffix}"
    if len(full) <= MAX_RUN_ID_LENGTH:
        return full

    digest = hashlib.sha256(safe_case.encode("utf-8")).hexdigest()[
        :_DIGEST_LENGTH
    ]
    # experiment + "." + prefix + "-" + digest + suffix == MAX_RUN_ID_LENGTH
    prefix_budget = (
        MAX_RUN_ID_LENGTH
        - len(safe_experiment)
        - len(suffix)
        - len(digest)
        - 2
    )
    if prefix_budget < _MIN_CASE_PREFIX:
        raise ValueError(
            "experiment-id and adapter id leave no room for a case name "
            f"within the {MAX_RUN_ID_LENGTH}-character run id limit "
            f"(experiment={len(safe_experiment)} chars, "
            f"suffix={len(suffix)} chars); use a shorter experiment-id."
        )
    return f"{safe_experiment}.{safe_case[:prefix_budget]}-{digest}{suffix}"
