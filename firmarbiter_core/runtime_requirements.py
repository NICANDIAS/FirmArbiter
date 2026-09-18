# firmarbiter_core/runtime_requirements.py
"""
Runtime capability registry (Onboarding Reliability Phase 3).

Two separate questions were being conflated before this file existed:

  1. What runtime requirement NAMES does the Adapter Contract recognise?
     -> the 'requirements' enum in schemas/adapter-manifest-v1.schema.json
        and schemas/run-request-v1.schema.json (kept in sync by
        tests/test_contract_consistency.py).

  2. Which of those does a given BACKEND actually implement, and how
     maturely?
     -> that lived nowhere explicit. A requirement can be a perfectly
        valid, schema-accepted thing for an adapter to declare while
        having zero backing implementation -- discoverable only by
        actually trying to run a candidate and hitting
        RuntimeRequirementError mid-run. (nested-containers was exactly
        this for a while; it now has a real implementation, at
        EXPERIMENTAL maturity -- see its entry below for what's still
        open. The general problem this file solves outlives any one
        requirement's current status.)

This file is the second answer, kept as data so it can be checked by a
test (see tests/test_runtime_requirements.py) instead of drifting the
way the requirements enum itself already drifted more than once
(docker-socket, nested-containers, loop-device-partitions) before
anyone wrote it down anywhere.

Status meanings:
  SUPPORTED     - implemented, used in real onboarded adapters, treat as
                  reliable.
  EXPERIMENTAL  - implemented and testable, but not yet proven across a
                  real onboarded candidate's full lifecycle end-to-end
                  (e.g. resource accounting, network-isolation semantics,
                  or cleanup still have known open gaps). A manifest
                  declaring this SHOULD be allowed to run, but the
                  person onboarding it should see the caveat before
                  they burn time debugging something that isn't their
                  candidate's fault.
  UNSUPPORTED   - the backend has no implementation at all. A manifest
                  declaring this is schema-valid but WILL fail at
                  container-creation time; catching this before a build
                  even starts is the entire point of this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CapabilityStatus(str, Enum):
    SUPPORTED = "supported"
    EXPERIMENTAL = "experimental"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class RequirementStatus:
    status: CapabilityStatus
    note: str


# The canonical requirement vocabulary. Must exactly match the
# 'requirements' enum in both schemas/adapter-manifest-v1.schema.json and
# schemas/run-request-v1.schema.json -- tests/test_contract_consistency.py
# already asserts those two schemas agree with each other; a further test
# in that same file asserts this dict's keys match them too, so adding a
# new requirement to the schemas without an entry here fails a test
# instead of silently having no documented backend status.
DOCKER_BACKEND_CAPABILITIES: dict[str, RequirementStatus] = {
    "kvm": RequirementStatus(
        CapabilityStatus.SUPPORTED,
        "Grants /dev/kvm. Used by FirmAE, FIRMADYNE.",
    ),
    "loop-devices": RequirementStatus(
        CapabilityStatus.SUPPORTED,
        "Device-cgroup rule for the whole loop major number (not a fixed "
        "node list -- see docker_backend.py for why). Used by FirmAE, "
        "FIRMADYNE.",
    ),
    "device-mapper": RequirementStatus(
        CapabilityStatus.SUPPORTED,
        "Grants /dev/mapper access.",
    ),
    "tun-tap": RequirementStatus(
        CapabilityStatus.SUPPORTED,
        "Grants /dev/net/tun. Used for candidate-created network "
        "interfaces (FirmAE, FIRMADYNE TAP devices).",
    ),
    "net-admin": RequirementStatus(
        CapabilityStatus.SUPPORTED,
        "Grants NET_ADMIN capability.",
    ),
    "ptrace": RequirementStatus(
        CapabilityStatus.SUPPORTED,
        "Grants SYS_PTRACE capability.",
    ),
    "docker-socket": RequirementStatus(
        CapabilityStatus.SUPPORTED,
        "Sibling-container (DooD) pattern: mounts the HOST's real Docker "
        "socket into the candidate. Used by fact_extractor. Deliberate, "
        "documented isolation exception -- the candidate gets "
        "root-equivalent control of the host's Docker daemon, scoped "
        "only to adapters that declare this.",
    ),
    "full-privileged": RequirementStatus(
        CapabilityStatus.SUPPORTED,
        "Full --privileged. Broadest possible grant.",
    ),
    "nested-containers": RequirementStatus(
        CapabilityStatus.EXPERIMENTAL,
        "Real, working implementation merged (adapters/greenhouse's "
        "onboarding branch): create_container() no longer raises -- it "
        "creates a per-run private network, a DinD sidecar, waits for "
        "the inner daemon to actually be ready (polls a real API call, "
        "not just container state), wires DOCKER_HOST into the "
        "candidate, and rolls back everything it created if any setup "
        "step fails. Teardown also now explicitly stops/removes the "
        "sidecar and force-disconnects the network, independent of "
        "whether the candidate's own container was removed. "
        "Confirmed still open, checked directly against the merged "
        "code: (1) no real nested child-container workload has been "
        "exercised end-to-end -- the test suite's own mock adapter "
        "(tests/fixtures/docker-adapters/mock-nested-containers-"
        "adapter/) only proves `docker version` reachability against "
        "DOCKER_HOST, never a `container create` inside the nested "
        "daemon; (2) the docker:dind image is still referenced by tag, "
        "not a pinned @sha256 digest -- the merged code's own comment "
        "says so explicitly, calling this out against the project's "
        "own reproducibility standard. Not independently re-checked "
        "since this update: whether the neutral reachability probe "
        "reaches inside the DinD network, and whether per-run resource "
        "accounting covers the sidecar or its children -- treat those "
        "as open until someone actually looks, not as resolved.",
    ),
    "loop-device-partitions": RequirementStatus(
        CapabilityStatus.SUPPORTED,
        "Bind-mounts the host's real /dev into the container "
        "(-v /dev:/dev). Deliberately separate from loop-devices: that "
        "requirement grants /dev/loop-control plus a device-cgroup rule "
        "without sharing the host's actual /dev directory. But a "
        "container's /dev is normally its own isolated devtmpfs even "
        "under --privileged, so partition sub-nodes the kernel creates "
        "dynamically after losetup -P (e.g. /dev/loop16p1) never become "
        "visible inside the container under that lighter model -- "
        "confirmed live during Greenhouse onboarding: its losetup step "
        "failed with exactly this symptom under --privileged alone, "
        "and only succeeded once /dev was bind-mounted directly. A "
        "materially more invasive grant than loop-devices (the whole "
        "host device tree, not just loop devices), so it's its own "
        "opt-in requirement.",
    ),
}


def status_for(requirement: str) -> RequirementStatus:
    """
    Look up a single requirement's status on the Docker backend.
    Raises KeyError for a name not in the registry at all (as opposed to
    one that's UNSUPPORTED, which IS in the registry -- KeyError here
    means the registry itself is out of date, which the consistency test
    should already have caught before this ever runs against real data).
    """
    return DOCKER_BACKEND_CAPABILITIES[requirement]


def unsupported_requirements(requirements: list[str]) -> list[str]:
    """Given a manifest's declared requirements list, return the subset
    that the Docker backend cannot currently run at all. Empty list means
    nothing here will hard-fail at container-creation time on that
    account (EXPERIMENTAL requirements are NOT included -- they'll run,
    just with caveats; see experimental_requirements())."""
    return [
        r for r in requirements
        if status_for(r).status is CapabilityStatus.UNSUPPORTED
    ]


def experimental_requirements(requirements: list[str]) -> list[str]:
    """Given a manifest's declared requirements list, return the subset
    that will run but aren't yet proven reliable end-to-end."""
    return [
        r for r in requirements
        if status_for(r).status is CapabilityStatus.EXPERIMENTAL
    ]
