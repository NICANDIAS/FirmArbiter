"""
probe_cleanup.py
----------------
Checks whether the environment is clean after a tool run. Specifically
looks for three things that cause subsequent runs to fail or produce
inaccurate results:

  1. Stale TAP network interfaces — these cause TUNSETIFF "Device or
     resource busy" errors on the next run and mean the second run's
     network state is not clean.

  2. Orphaned QEMU processes — these consume CPU and memory and can
     interfere with subsequent runs.

  3. Leftover VERITAS containers — containers started by VERITAS that
     were not stopped cleanly. Identified by the "veritas/" image prefix.

If anything is found, it is recorded in the result and then forcibly
removed so the next run always starts from a clean state regardless of
how messy the previous tool was. The fact that cleanup was needed is
itself a finding about the tool.
"""

import subprocess
import re


def _get_stale_tap_devices() -> list:
    """Return names of TAP interfaces currently present on the host."""
    try:
        output = subprocess.check_output(
            ["ip", "link", "show"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return []
    return re.findall(r'\d+:\s+(tap\w+):', output)


def _get_orphan_qemu_pids() -> list:
    """Return PIDs of any QEMU processes still running on the host."""
    try:
        output = subprocess.check_output(
            ["pgrep", "-f", "qemu"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return [int(p.strip()) for p in output.strip().splitlines()
                if p.strip().isdigit()]
    except subprocess.CalledProcessError:
        return []


def _get_leftover_veritas_containers() -> list:
    """
    Return IDs of Docker containers started from a VERITAS image
    (images named veritas/<candidate>) that are still running.

    We only report containers from VERITAS images — not any other
    containers that may legitimately be running on the machine.
    """
    try:
        output = subprocess.check_output(
            ["docker", "ps", "--format", "{{.ID}} {{.Image}}"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        leftover = []
        for line in output.strip().splitlines():
            parts = line.strip().split()
            if len(parts) >= 2:
                container_id = parts[0]
                image_name   = parts[1]
                if image_name.startswith("veritas/"):
                    leftover.append(container_id)
        return leftover
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def _remove_tap_devices(tap_names: list):
    """Remove stale TAP interfaces. Requires root."""
    for name in tap_names:
        subprocess.run(
            ["ip", "link", "delete", name],
            stderr=subprocess.DEVNULL,
        )


def _kill_qemu_processes(pids: list):
    """Send SIGKILL to orphaned QEMU processes."""
    for pid in pids:
        subprocess.run(
            ["kill", "-9", str(pid)],
            stderr=subprocess.DEVNULL,
        )


def _stop_veritas_containers(container_ids: list):
    """Force-stop leftover VERITAS containers."""
    for cid in container_ids:
        subprocess.run(
            ["docker", "stop", "--time", "5", cid],
            capture_output=True,
            timeout=15,
        )


def _cleanup_stale_loop_devices():
    """
    Detach any loop devices left behind by FirmAE.
    FirmAE does not clean up loop devices on exit — Finding F7.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["sudo", "losetup", "-l"],
            capture_output=True, text=True, timeout=10
        )
        stale = []
        for line in result.stdout.splitlines():
            if "firmae" in line.lower() or "(deleted)" in line:
                dev = line.split()[0]
                if dev.startswith("/dev/loop"):
                    stale.append(dev)
        for dev in stale:
            subprocess.run(["sudo", "losetup", "-d", dev],
                           capture_output=True, timeout=5)
        return stale
    except Exception:
        return []


def probe_cleanup(candidate_name: str, force_clean: bool = True) -> dict:
    """
    Check post-run environment and optionally force cleanup.

    Parameters
    ----------
    candidate_name : str
        Name of the tool that just ran, used for logging only.
    force_clean : bool
        If True, remove stale TAP devices, kill orphan QEMU processes,
        and stop leftover VERITAS containers after recording them.

    Returns
    -------
    dict
        Matches the 'cleanup' block in result_schema.json.
    """
    stale_taps          = _get_stale_tap_devices()
    orphan_qemus        = _get_orphan_qemu_pids()
    leftover_containers = _get_leftover_veritas_containers()

    result = {
        "stale_tap_devices":        stale_taps,
        "orphan_qemu_pids":         orphan_qemus,
        "orphan_docker_containers": leftover_containers,
        "cleanup_clean": (
            len(stale_taps)          == 0 and
            len(orphan_qemus)        == 0 and
            len(leftover_containers) == 0
        ),
    }

    if force_clean:
        if stale_taps:
            _remove_tap_devices(stale_taps)
        if orphan_qemus:
            _kill_qemu_processes(orphan_qemus)
        if leftover_containers:
            _stop_veritas_containers(leftover_containers)

    return result
