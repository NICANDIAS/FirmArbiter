"""
probe_boot.py
-------------
Wraps a candidate tool invocation inside a Docker container, monitors its
resource usage from the host, and detects whether the firmware reached a
stable running state.

Each run gets a fresh container built from the candidate's image. The image
is built automatically on first use. This ensures that every tool runs in a
clean environment with only its own declared dependencies, so no tool's
installation can affect another tool's results.

The container shares the host network stack (--network host) because FirmAE
and FIRMADYNE create TAP interfaces that VERITAS's service probes need to
reach from the host side. VERITAS monitors the container from outside — it
never runs code inside the container itself.

Resource monitoring (CPU, RAM) reads from the Docker container's stats API
rather than the process tree, because the tool processes are inside the
container and not directly visible to psutil on the host.
"""

import os
import re
import subprocess
import threading
import time
import sys
from pathlib import Path

from docker_runner import DockerRunner


# How often to sample container resource usage, in seconds
MONITOR_INTERVAL = 2

# Stall detection — if tool CPU stays below 1% for this many seconds,
# it is considered stuck and will be killed rather than waiting for
# the full 3-hour ceiling. This prevents wasting hours on incompatible firmware.
# Reads from veritas.conf STALL_TIMEOUT_SECONDS if available, else 600s.
import os as _os
STALL_CPU_THRESHOLD_SECONDS = int(_os.environ.get("STALL_TIMEOUT_SECONDS", "600"))

# Hard ceiling — absolute maximum seconds before killing any run
# Set high so genuinely working tools are not cut off artificially
HARD_CEILING_SECONDS = 10800  # 3 hours

# Stall threshold — if CPU has been near zero for this many seconds,
# the tool is stuck and should be killed
STALL_CPU_THRESHOLD_SECONDS = 600  # 10 minutes of near-zero CPU = stuck


def _monitor_container_resources(container_id: str, samples: list,
                                  stop_event: threading.Event):
    """
    Background thread: read CPU and memory stats from the container
    every MONITOR_INTERVAL seconds using docker stats.

    Appends (timestamp, cpu_percent, rss_mb) to samples.
    """
    while not stop_event.is_set():
        try:
            result = subprocess.run(
                [
                    "docker", "stats", container_id,
                    "--no-stream",
                    "--format", "{{.CPUPerc}},{{.MemUsage}}",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                line = result.stdout.strip()
                # Format: "12.34%,210.5MiB / 15.6GiB"
                parts = line.split(",")
                if len(parts) >= 2:
                    cpu_str  = parts[0].replace("%", "").strip()
                    mem_str  = parts[1].split("/")[0].strip()

                    cpu = float(cpu_str) if cpu_str else 0.0

                    # Parse memory — docker reports in B, KiB, MiB, GiB
                    rss_mb = 0.0
                    mem_match = re.match(r"([\d.]+)\s*([A-Za-z]*)", mem_str)
                    if mem_match:
                        val  = float(mem_match.group(1))
                        unit = mem_match.group(2).upper()
                        if "GIB" in unit or "GB" in unit:
                            rss_mb = val * 1024
                        elif "MIB" in unit or "MB" in unit:
                            rss_mb = val
                        elif "KIB" in unit or "KB" in unit:
                            rss_mb = val / 1024
                        else:
                            rss_mb = val / (1024 * 1024)

                    samples.append((time.monotonic(), cpu, rss_mb))
        except Exception:
            pass

        stop_event.wait(MONITOR_INTERVAL)


def _parse_reported_ip(line: str) -> str | None:
    """
    Extract an IPv4 address from a line of tool output.
    Handles all common formats:
        [+] Network reachable on 192.168.0.1!
        [+] Web service on 192.168.0.1
        http://192.168.0.1/
        Listening on 192.168.1.1:80

    Excludes loopback (127.x) and Docker bridge addresses (172.17.x)
    since those are host addresses, not emulated firmware addresses.
    Returns the best matching IP or None.
    """
    matches = re.findall(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b', line)
    # Filter out addresses that are definitely host addresses
    firmware_ips = [
        ip for ip in matches
        if not ip.startswith("127.")
        and not ip.startswith("172.17.")
        and not ip.startswith("172.18.")
        and not ip == "0.0.0.0"
    ]
    return firmware_ips[-1] if firmware_ips else None


def _probe_boot_direct(
    firmware_path, architecture, timeout_seconds,
    success_signals, stdout_log, stderr_log,
    output_dir, candidate_dir, tool_local_path
) -> dict:
    """
    Run the candidate tool directly on the host VM without Docker.
    Used when the tool requires kernel features unavailable in containers.
    VERITAS monitors from outside using psutil — same as Docker mode.
    """
    import psutil
    import threading

    result = {
        "success":             False,
        "wall_time_seconds":   None,
        "cpu_seconds":         0.0,
        "peak_ram_mb":         0.0,
        "timeout_seconds":     timeout_seconds,
        "timed_out":           False,
        "failure_reason":      None,
        "reported_ip":         None,
        "tool_version":        "direct",
        "was_working_at_stop": None,
        "detection_method":    None,
    }

    adapter = Path(candidate_dir) / "run_adapter.sh"
    if not adapter.exists():
        result["failure_reason"] = "adapter_not_found"
        return result

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    lower_signals = [s.lower().strip() for s in success_signals]
    cmd           = ["sudo", "-n", "timeout", "--kill-after=30s", f"{int(timeout_seconds)}s", "bash", str(adapter), firmware_path, architecture]
    resource_samples = []
    boot_detected    = False
    start_time       = time.monotonic()

    try:
        with open(stdout_log, "w") as fout, open(stderr_log, "w") as ferr:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=ferr,
                text=True, bufsize=1,
            )

            def _monitor():
                try:
                    ps = psutil.Process(proc.pid)
                    while proc.poll() is None:
                        try:
                            all_procs = [ps] + ps.children(recursive=True)
                            cpu = sum(p.cpu_percent(interval=0)
                                      for p in all_procs)
                            mem = sum(p.memory_info().rss
                                      for p in all_procs) / 1024 / 1024
                            resource_samples.append(
                                (time.monotonic(), cpu, mem))
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                        time.sleep(MONITOR_INTERVAL)
                except Exception:
                    pass

            threading.Thread(target=_monitor, daemon=True).start()

            while True:
                elapsed = time.monotonic() - start_time

                line = proc.stdout.readline()
                elapsed = time.monotonic() - start_time
                if line:
                    fout.write(line)
                    fout.flush()
                    lower_line  = line.lower().strip()
                    detected_ip = _parse_reported_ip(line)
                    reachability_keywords = [
                        "reachable", "network reachable", "web service",
                        "http://", "https://", "service on",
                    ]
                    ip_with_keyword = (
                        detected_ip and
                        any(kw in lower_line for kw in reachability_keywords)
                    )
                    explicit_signal = any(
                        s in lower_line for s in lower_signals)

                    if ip_with_keyword or explicit_signal:
                        result["success"]           = True
                        result["wall_time_seconds"] = round(elapsed, 2)
                        result["reported_ip"]       = detected_ip
                        result["detection_method"]  = (
                            "ip_with_keyword" if ip_with_keyword
                            else "explicit_signal"
                        )
                        boot_detected = True

                if boot_detected:
                    break

                if proc.poll() is not None and not boot_detected:
                    result["wall_time_seconds"] = round(elapsed, 2)

                    if proc.returncode in (124, 137) or elapsed >= timeout_seconds:
                        result["timed_out"] = True
                        result["failure_reason"] = "timeout"
                    else:
                        result["failure_reason"] = f"tool_exited_without_success_rc_{proc.returncode}"

                    break

                if elapsed >= timeout_seconds:
                    result["timed_out"]         = True
                    result["wall_time_seconds"] = round(elapsed, 2)
                    if resource_samples:
                        last_n  = resource_samples[
                            -min(10, len(resource_samples)):]
                        avg_cpu = sum(s[1] for s in last_n) / len(last_n)
                        if avg_cpu > 1.0:
                            result["failure_reason"]      = "timeout_while_working"
                            result["was_working_at_stop"] = True
                            print(f"[VERITAS]        *** Tool was WORKING "
                                  f"when stopped (CPU {avg_cpu:.1f}%)")
                        else:
                            result["failure_reason"]      = "timeout_while_idle"
                            result["was_working_at_stop"] = False
                            print(f"[VERITAS]        *** Tool was IDLE "
                                  f"when stopped (CPU {avg_cpu:.1f}%)")
                    else:
                        result["failure_reason"] = "timeout"
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break

                elapsed_int = int(elapsed)
                if elapsed_int > 0 and elapsed_int % 30 == 0:
                    if resource_samples:
                        last_n  = resource_samples[
                            -min(5, len(resource_samples)):]
                        avg_cpu = sum(s[1] for s in last_n) / len(last_n)
                        status  = "working" if avg_cpu > 1.0 else "IDLE"
                        remaining = int(timeout_seconds - elapsed)
                        print(f"[VERITAS]        ... {elapsed_int}s elapsed  "
                              f"cpu={avg_cpu:.1f}%  status={status}  "
                              f"{remaining}s remaining")

                time.sleep(0.5)

            proc.wait(timeout=10)

    except Exception as exc:
        result["failure_reason"] = f"execution_error: {exc}"

    total_elapsed = time.monotonic() - start_time
    if result["wall_time_seconds"] is None:
        result["wall_time_seconds"] = round(total_elapsed, 2)

    if resource_samples:
        result["cpu_seconds"] = round(
            sum(s[1] * MONITOR_INTERVAL / 100.0 for s in resource_samples), 2)
        result["peak_ram_mb"] = round(
            max(s[2] for s in resource_samples), 1)
        if not result.get("success") and "was_working_at_stop" not in result:
            last_n  = resource_samples[-min(10, len(resource_samples)):]
            avg_cpu = sum(s[1] for s in last_n) / len(last_n)
            result["was_working_at_stop"] = avg_cpu > 1.0

    return result


def probe_boot(
    candidate_dir: str | Path,
    firmware_path: str,
    architecture: str,
    success_signals: list,
    timeout_seconds: int = 300,
    stdout_log_path: str | None = None,
    stderr_log_path: str | None = None,
    output_dir: str | None = None,
    build_timeout: int = 1800,
    build_stall_timeout: int = 120,
) -> dict:
    """
    Run a candidate tool inside a Docker container and measure what happens.

    Parameters
    ----------
    candidate_dir : str or Path
        Path to the candidate's folder inside candidates/. Used to find
        the Dockerfile and adapter script.
    firmware_path : str
        Full path to the firmware image on the host.
    architecture : str
        Architecture hint passed to the tool.
    success_signals : list of str
        Strings to watch for in the container's stdout. Case-insensitive.
    timeout_seconds : int
        How long to wait before declaring a timeout.
    stdout_log_path : str or None
        Where to write the container's stdout.
    stderr_log_path : str or None
        Not used directly (Docker combines stdout/stderr) but kept for
        interface compatibility.
    output_dir : str or None
        Directory mounted into the container at /veritas_output.
        Created automatically if not provided.

    Returns
    -------
    dict
        Matches the 'boot' block in result_schema.json, plus a
        'reported_ip' key extracted from the tool's success signal line,
        and a 'tool_version' key with the git commit from the image.
    """
    result = {
        "success":           False,
        "wall_time_seconds": None,
        "cpu_seconds":       None,
        "peak_ram_mb":       None,
        "timeout_seconds":   timeout_seconds,
        "timed_out":         False,
        "failure_reason":    None,
        "reported_ip":       None,
        "tool_version":      "unknown",
    }

    candidate_dir = Path(candidate_dir)
    # Check for direct execution mode
    candidate_conf = Path(candidate_dir) / "candidate.conf"
    tool_source = "docker"
    tool_local_path = None
    if candidate_conf.exists():
        for line in candidate_conf.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k == "tool_source":
                    tool_source = v
                if k == "tool_local_path":
                    tool_local_path = v

    tool_source = str(tool_source).strip().lower()
    print(
        f"[VERITAS DEBUG] candidate_conf={candidate_conf} "
        f"exists={candidate_conf.exists()} "
        f"tool_source={tool_source!r} candidate_dir={candidate_dir}",
        flush=True,
    )
    if tool_source == "direct":
        return _probe_boot_direct(
            firmware_path=firmware_path,
            architecture=architecture,
            timeout_seconds=timeout_seconds,
            success_signals=success_signals,
            stdout_log=stdout_log_path or str(Path(output_dir or ".") / "direct_stdout.txt"),
            stderr_log=stderr_log_path or str(Path(output_dir or ".") / "direct_stderr.txt"),
            output_dir=output_dir,
            candidate_dir=str(candidate_dir),
            tool_local_path=tool_local_path,
        )

    runner = DockerRunner(candidate_dir)

    # ── Ensure the Docker image exists ────────────────────────────────────────
    build_log = None
    if stdout_log_path:
        build_log = stdout_log_path.replace("stdout", "build")

    if not runner.ensure_image(build_log_path=build_log,
                               build_timeout=build_timeout,
                               stall_timeout=build_stall_timeout):
        result["failure_reason"] = "docker_image_build_failed"
        return result

    result["tool_version"] = runner.get_image_commit()

    # ── Prepare output directory ──────────────────────────────────────────────
    if output_dir is None:
        output_dir = str(Path("results/container_output") /
                         f"{candidate_dir.name}_{int(time.time())}")
    output_dir = str(Path(output_dir).resolve())
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    if stdout_log_path:
        Path(stdout_log_path).parent.mkdir(parents=True, exist_ok=True)

    lower_signals = [s.lower() for s in success_signals]
    start_time = time.monotonic()

    # ── Start container and monitor it ────────────────────────────────────────
    with runner.run_container(
        firmware_path=firmware_path,
        architecture=architecture,
        output_dir=output_dir,
        stdout_log_path=stdout_log_path,
    ) as container_id:

        if container_id is None:
            result["failure_reason"] = "container_start_failed"
            return result

        # Start resource monitor thread
        resource_samples = []
        stop_monitor = threading.Event()
        monitor_thread = threading.Thread(
            target=_monitor_container_resources,
            args=(container_id, resource_samples, stop_monitor),
            daemon=True,
        )
        monitor_thread.start()

        # Poll container logs for success signal until timeout
        boot_detected = False
        log_position  = 0

        while True:
            elapsed = time.monotonic() - start_time

            if elapsed >= timeout_seconds:
                result["timed_out"]         = True
                result["wall_time_seconds"] = round(elapsed, 2)
                # Classify whether the tool was working or idle at stop time
                if resource_samples:
                    last_n  = resource_samples[-min(10, len(resource_samples)):]
                    avg_cpu = sum(s[1] for s in last_n) / len(last_n)
                    if avg_cpu > 1.0:
                        result["failure_reason"]      = "timeout_while_working"
                        result["was_working_at_stop"] = True
                        print(f"[VERITAS]        *** Tool was WORKING when stopped "
                              f"(avg CPU {avg_cpu:.1f}% — may have succeeded with more time)")
                    else:
                        result["failure_reason"]      = "timeout_while_idle"
                        result["was_working_at_stop"] = False
                        print(f"[VERITAS]        *** Tool was IDLE when stopped "
                              f"(avg CPU {avg_cpu:.1f}% — likely stuck or incompatible firmware)")
                else:
                    result["failure_reason"]      = "timeout"
                    result["was_working_at_stop"] = None
                break

            # Check whether the container is still running
            if not runner.is_container_running(container_id):
                # Container exited — check logs one more time for a late signal
                pass

            # Read new log output since last check
            logs = runner.get_container_logs(container_id)
            new_lines = logs[log_position:].splitlines()
            log_position = len(logs)

            for line in new_lines:
                lower_line = line.lower().strip()

                # Dynamic detection — check for IP address patterns that
                # indicate network reachability regardless of exact wording.
                # This means VERITAS works even if tool output format changes.
                detected_ip = _parse_reported_ip(line)

                # Pattern 1: IP address appears after reachability keywords
                reachability_keywords = [
                    "reachable", "network reachable", "web service",
                    "http://", "https://", "listening on", "service on",
                    "emulation success", "boot success", "interface up"
                ]
                ip_with_keyword = (
                    detected_ip and
                    any(kw in lower_line for kw in reachability_keywords)
                )

                # Pattern 2: Explicit success signal from candidate.conf
                explicit_signal = any(
                    signal in lower_line for signal in lower_signals
                )

                # Pattern 3: Tool exit code 0 with an IP seen earlier
                # (handled after the loop)

                if ip_with_keyword or explicit_signal:
                    result["success"]           = True
                    result["wall_time_seconds"] = round(elapsed, 2)
                    result["reported_ip"]       = detected_ip
                    result["detection_method"]  = (
                        "ip_with_keyword" if ip_with_keyword else "explicit_signal"
                    )
                    boot_detected = True
                    break

                if boot_detected:
                    break

            if boot_detected:
                break

            # If container exited without a success signal, it failed
            if not runner.is_container_running(container_id) and not boot_detected:
                result["failure_reason"]    = "tool_exited_without_success"
                result["wall_time_seconds"] = round(elapsed, 2)
                break

            # Heartbeat every 30 seconds with CPU status and stall detection
            elapsed_int = int(elapsed)
            remaining   = int(timeout_seconds - elapsed)
            if elapsed_int > 0 and elapsed_int % 30 == 0:
                if resource_samples:
                    last_n  = resource_samples[-min(5, len(resource_samples)):]
                    avg_cpu = sum(s[1] for s in last_n) / len(last_n)
                    status  = "working" if avg_cpu > 1.0 else "IDLE"
                    print(f"[VERITAS]        ... {elapsed_int}s elapsed  "
                          f"cpu={avg_cpu:.1f}%  status={status}  "
                          f"{remaining}s remaining")

                    # Stall detection — if tool has been idle for too long, kill it
                    # Check ALL recent samples, not just the last 5
                    stall_window = min(len(resource_samples),
                                       STALL_CPU_THRESHOLD_SECONDS // MONITOR_INTERVAL)
                    stall_samples = resource_samples[-stall_window:]
                    all_idle = all(s[1] < 1.0 for s in stall_samples)
                    enough_samples = len(stall_samples) >= 10  # at least 20s of data

                    if all_idle and enough_samples and elapsed_int >= 60:
                        idle_seconds = len(stall_samples) * MONITOR_INTERVAL
                        print(f"[VERITAS]        *** STALL DETECTED — tool has been "
                              f"idle for {idle_seconds}s (CPU < 1% throughout)")
                        print(f"[VERITAS]        *** Killing — firmware likely "
                              f"incompatible with this tool")
                        result["timed_out"]           = False
                        result["failure_reason"]      = "stall_detected_idle"
                        result["was_working_at_stop"] = False
                        result["wall_time_seconds"]   = round(elapsed, 2)
                        # Signal the outer loop to break
                        boot_detected = False
                        break
                else:
                    print(f"[VERITAS]        ... {elapsed_int}s elapsed  "
                          f"cpu=?  {remaining}s remaining")

            time.sleep(2)

        stop_monitor.set()
        monitor_thread.join(timeout=5)

    # ── Docker/saved-log fallback success scan ────────────────────────────────
    # In Docker mode the container may exit quickly after printing the success
    # line. If docker logs polling misses that final line, the background log
    # writer may still have captured it in stdout_log_path. Therefore, before
    # declaring failure, scan the saved stdout file once.
    if not result.get("success"):
        try:
            from pathlib import Path as _VeritasPath

            _stdout_path = locals().get("stdout_log_path")
            _signals = locals().get("lower_signals", [])

            if _stdout_path and _VeritasPath(_stdout_path).exists():
                _fallback_text = _VeritasPath(_stdout_path).read_text(errors="replace")

                for _line in _fallback_text.splitlines():
                    _lower_line = _line.lower().strip()
                    _detected_ip = _parse_reported_ip(_line)

                    _reachability_keywords = [
                        "reachable", "network reachable", "web service",
                        "http://", "https://", "listening on", "service on",
                        "emulation success", "boot success", "interface up"
                    ]

                    _ip_with_keyword = (
                        _detected_ip and
                        any(_kw in _lower_line for _kw in _reachability_keywords)
                    )

                    _explicit_signal = any(
                        _signal in _lower_line for _signal in _signals
                    )

                    if _ip_with_keyword or _explicit_signal:
                        result["success"] = True
                        result["reported_ip"] = _detected_ip
                        result["failure_reason"] = None
                        result["detection_method"] = (
                            "stdout_log_fallback_ip_with_keyword"
                            if _ip_with_keyword else
                            "stdout_log_fallback_explicit_signal"
                        )
                        result["timed_out"] = False
                        result["was_working_at_stop"] = None
                        break

        except Exception as _exc:
            # Do not fail the benchmark because the fallback scan failed.
            # Store the note only if this result schema has a notes list.
            if isinstance(result.get("notes"), list):
                result["notes"].append(f"stdout_log_fallback_scan_failed: {_exc}")


    # ── Record total wall time if not already set ─────────────────────────────
    total_elapsed = time.monotonic() - start_time
    if result["wall_time_seconds"] is None:
        result["wall_time_seconds"] = round(total_elapsed, 2)

    # ── Post-run working/idle classification (for non-timeout exits) ──────────
    if resource_samples and not result.get("success") and        "was_working_at_stop" not in result:
        last_n  = resource_samples[-min(10, len(resource_samples)):]
        avg_cpu = sum(s[1] for s in last_n) / len(last_n)
        result["was_working_at_stop"] = avg_cpu > 1.0

    # ── Summarise resource samples ────────────────────────────────────────────
    if resource_samples:
        cpu_seconds = sum(
            s[1] * MONITOR_INTERVAL / 100.0
            for s in resource_samples
        )
        result["cpu_seconds"]  = round(cpu_seconds, 2)
        result["peak_ram_mb"]  = round(max(s[2] for s in resource_samples), 1)
    else:
        result["cpu_seconds"]  = 0.0
        result["peak_ram_mb"]  = 0.0

    return result
