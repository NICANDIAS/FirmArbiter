"""
probe_stability.py
------------------
Waits 60 seconds after a tool declares success and then re-runs the
service probe to check whether the emulated service is still running.

This matters because some tools declare success as soon as the network
interface comes up, before the firmware's web server has fully started —
or conversely, the tool cleans up the emulation environment shortly
after declaring success, leaving the service unreachable moments later.

Either case is a meaningful finding: a service that is only briefly
reachable is not useful for downstream analysis such as fuzzing or
exploit validation.

Usage (called from run_veritas.py after probe_service succeeds):

    from probes.probe_stability import probe_stability
    result = probe_stability(
        reported_ip="192.168.0.1",
        reported_port=80,
        wait_seconds=60,
    )
"""

import time
from probes.probe_service import probe_service


def probe_stability(
    reported_ip: str,
    reported_port: int = 80,
    wait_seconds: int = 60,
) -> dict:
    """
    Wait wait_seconds then re-run the service probe.

    Parameters
    ----------
    reported_ip : str
        The IP address that passed the initial service probe.
    reported_port : int
        The port that passed the initial service probe.
    wait_seconds : int
        How long to wait before the re-check. Default 60.

    Returns
    -------
    dict
        A dict matching the 'stability' block in result_schema.json.
    """
    result = {
        "probe_attempted": False,
        "service_reachable_at_60s": None,
        "service_authentic_at_60s": None,
    }

    if not reported_ip:
        return result

    result["probe_attempted"] = True

    # The wait is intentional — we are checking for persistence, not
    # just presence. Sleeping here is the correct behaviour.
    time.sleep(wait_seconds)

    follow_up = probe_service(reported_ip=reported_ip, reported_port=reported_port)
    result["service_reachable_at_60s"] = follow_up["service_reachable"]
    result["service_authentic_at_60s"] = follow_up["service_authentic"]

    return result
