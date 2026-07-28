"""
docker_runner.py
----------------
Handles all Docker operations for FIRMARBITER.

Supports two tool source modes, set when the tool is registered:

  git mode   — Docker builds the image by cloning the tool from its
               repository URL. Works for any publicly accessible repo.
               The exact git commit is recorded in the result JSON.

  local mode — Docker builds the image by copying the tool from a folder
               on your host machine. Use this for private repositories,
               offline environments, or when you want to test a locally
               modified version of the tool.

FIRMARBITER selects the correct mode automatically based on how the tool
was registered. You do not configure this manually.

The firmware folder is mounted read-only into every container so tools
can read images but cannot modify the corpus. FIRMARBITER's probes run on
the host and observe containers from the outside.
"""

import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path


CONTAINER_FIRMWARE_DIR  = "/firmware"
CONTAINER_OUTPUT_DIR    = "/firmarbiter_output"
IMAGE_PREFIX            = "firmarbiter"
CONTAINER_START_TIMEOUT = 30


def _read_candidate_conf(candidate_dir: Path) -> dict:
    """
    Read candidate.conf from the candidate directory and return a dict.
    Returns an empty dict if the file cannot be read.
    """
    conf_path = candidate_dir / "candidate.conf"
    settings  = {}
    try:
        with open(conf_path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                settings[key.strip()] = value.strip()
    except OSError:
        pass
    return settings


class DockerRunner:
    """
    Manages Docker image building and container lifecycle for one candidate.

    Reads tool_source, tool_repo, and tool_local_path from candidate.conf
    to decide whether to clone from a repo or copy from a local folder.
    """

    def __init__(self, candidate_dir: Path):
        self.candidate_dir = Path(candidate_dir)
        self.candidate_id  = candidate_dir.name
        self.image_name    = f"{IMAGE_PREFIX}/{self.candidate_id}"
        self.dockerfile    = self.candidate_dir / "Dockerfile"

        # Read source configuration from candidate.conf
        conf = _read_candidate_conf(self.candidate_dir)
        self.tool_source     = conf.get("tool_source", "git").strip().lower()
        self.tool_repo       = conf.get("tool_repo", "").strip()
        self.tool_local_path = conf.get("tool_local_path", "").strip()

    # ── Source mode helpers ───────────────────────────────────────────────────

    def _resolve_local_path(self) -> Path | None:
        """
        Return the resolved local tool path, or None if it cannot be found.
        Checks both the path stored in candidate.conf and a .local_path file
        written by firmarbiter_register.py at registration time.
        """
        # First check candidate.conf
        if self.tool_local_path and Path(self.tool_local_path).is_dir():
            return Path(self.tool_local_path).resolve()

        # Fall back to .local_path file written during registration
        local_path_file = self.candidate_dir / ".local_path"
        if local_path_file.exists():
            stored = local_path_file.read_text().strip()
            if stored and Path(stored).is_dir():
                return Path(stored).resolve()

        return None

    def _build_args_and_context(self) -> tuple[list, dict]:
        """
        Return (extra_docker_args, env_overrides) for the build command.

        For git mode:
            --build-arg TOOL_SOURCE=git
            --build-arg TOOL_REPO=<url>
            --build-context tool_src=/dev/null  (placeholder, not used)

        For local mode:
            --build-arg TOOL_SOURCE=local
            --build-context tool_src=/path/to/local/tool
        """
        if self.tool_source == "local":
            local_path = self._resolve_local_path()
            if local_path is None:
                print(
                    f"[FIRMARBITER][docker] ERROR: Local tool path not found for "
                    f"{self.candidate_id}.\n"
                    f"[FIRMARBITER][docker] Re-register with:\n"
                    f"[FIRMARBITER][docker]   python run_firmarbiter.py "
                    f"--register-tool /path/to/tool",
                    file=sys.stderr,
                )
                return None, {}

            print(f"[FIRMARBITER][docker] Source mode : local")
            print(f"[FIRMARBITER][docker] Local path  : {local_path}")
            # Pass local path as a build arg — compatible with legacy builder.
            # The Dockerfile uses TOOL_LOCAL_PATH to locate the tool.
            extra_args = [
                "--build-arg", "TOOL_SOURCE=local",
                "--build-arg", f"TOOL_LOCAL_PATH={local_path}",
            ]
            return extra_args, {}

        else:
            # git mode — clone from repo
            repo = self.tool_repo
            if not repo:
                print(
                    f"[FIRMARBITER][docker] ERROR: No tool_repo set in candidate.conf "
                    f"for {self.candidate_id} (required for git mode).",
                    file=sys.stderr,
                )
                return None, {}

            print(f"[FIRMARBITER][docker] Source mode : git")
            print(f"[FIRMARBITER][docker] Repo        : {repo}")
            extra_args = [
                "--build-arg", "TOOL_SOURCE=git",
                "--build-arg", f"TOOL_REPO={repo}",
            ]
            return extra_args, {}

    # ── Image management ──────────────────────────────────────────────────────

    def image_exists(self) -> bool:
        """Return True if the Docker image for this candidate already exists."""
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", self.image_name],
                capture_output=True, timeout=10,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def build_image(self, log_path: str | None = None,
                    no_cache: bool = False,
                    build_timeout: int = 1800,
                    stall_timeout: int = 120) -> bool:
        """
        Build the Docker image for this candidate.

        For git mode:   clones the tool from its repository.
        For local mode: copies the tool from the registered local folder.

        Parameters
        ----------
        build_timeout : int
            Maximum total seconds to wait for the entire build.
            Default 1800 (30 minutes). If hit, the build is killed and
            the tool is skipped for this run.
        stall_timeout : int
            Maximum seconds to wait without seeing any new output from
            Docker. Default 120 (2 minutes). Catches the case where the
            build is stuck waiting on a network connection that never
            arrives, such as a restricted college network.

        Returns True if the build succeeded.
        """
        if not self.dockerfile.exists():
            print(
                f"[FIRMARBITER][docker] ERROR: No Dockerfile at {self.dockerfile}.\n"
                f"[FIRMARBITER][docker] Run --register-tool to create it.",
                file=sys.stderr,
            )
            return False

        extra_args, _ = self._build_args_and_context()
        if extra_args is None:
            return False

        print(f"\n[FIRMARBITER][docker] Building image: {self.image_name}")
        if self.tool_source == "git":
            print(f"[FIRMARBITER][docker] This clones the tool from its repo and "
                  f"installs dependencies.")
            print(f"[FIRMARBITER][docker] Build timeout : {build_timeout}s total, "
                  f"{stall_timeout}s max without output.")
            print(f"[FIRMARBITER][docker] Subsequent runs use the cached image.")
        else:
            print(f"[FIRMARBITER][docker] Copying tool from local folder into image.")

        cmd = (
            ["docker", "build"]
            + (["--no-cache"] if no_cache else [])
            + ["--tag", self.image_name]
            + extra_args
            + ["--file", str(self.dockerfile)]
            + [str(self.candidate_dir)]
        )

        log_fh   = open(log_path, "w") if log_path else None
        success  = False
        killed   = False

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            build_start      = time.time()
            last_output_time = time.time()

            # Read output line by line in a non-blocking way using select
            # so we can enforce both the stall timeout and the total timeout
            # without threads.
            import select

            while True:
                # Check total build timeout
                elapsed = time.time() - build_start
                if elapsed >= build_timeout:
                    print(
                        f"\n[FIRMARBITER][docker] Build timeout ({build_timeout}s) "
                        f"reached for {self.candidate_id}. Killing.",
                        file=sys.stderr,
                    )
                    proc.kill()
                    killed = True
                    break

                # Check stall timeout — no output for stall_timeout seconds
                stall = time.time() - last_output_time
                if stall >= stall_timeout:
                    print(
                        f"\n[FIRMARBITER][docker] Build stalled — no output for "
                        f"{stall_timeout}s. This usually means the network is "
                        f"blocked (e.g. college/university firewall). Killing.",
                        file=sys.stderr,
                    )
                    print(
                        f"[FIRMARBITER][docker] Tip: run on a personal hotspot to "
                        f"build the image, then return to any network for runs.",
                        file=sys.stderr,
                    )
                    proc.kill()
                    killed = True
                    break

                # Check if there is output ready to read (wait up to 2s)
                ready, _, _ = select.select([proc.stdout], [], [], 2.0)
                if ready:
                    line = proc.stdout.readline()
                    if not line:
                        # EOF — process finished
                        break
                    last_output_time = time.time()
                    stripped = line.rstrip()
                    if log_fh:
                        log_fh.write(line)
                        log_fh.flush()
                    # Show meaningful progress lines in the terminal
                    if any(kw in stripped.lower() for kw in
                           ["step", "cloning", "installing", "copying",
                            "error", "warning", "fatal", "failed"]):
                        print(f"[FIRMARBITER][docker]   {stripped}")
                else:
                    # No output in the last 2s — check if process ended
                    if proc.poll() is not None:
                        break
                    # Still running but quiet — print a heartbeat every 30s
                    stall_so_far = int(time.time() - last_output_time)
                    if stall_so_far > 0 and stall_so_far % 30 == 0:
                        remaining = stall_timeout - stall_so_far
                        print(f"[FIRMARBITER][docker]   ... waiting "
                              f"({stall_so_far}s without output, "
                              f"will abort in {remaining}s if nothing happens)")

            if not killed:
                proc.wait(timeout=30)
                success = proc.returncode == 0

        except Exception as exc:
            print(f"[FIRMARBITER][docker] ERROR during build: {exc}", file=sys.stderr)
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            if log_fh:
                log_fh.close()

        if success:
            print(f"[FIRMARBITER][docker] Image built successfully: {self.image_name}")
        elif killed:
            print(
                f"[FIRMARBITER][docker] Build killed due to timeout for "
                f"{self.candidate_id}. Skipping this tool for now.",
                file=sys.stderr,
            )
        else:
            print(
                f"[FIRMARBITER][docker] ERROR: Build failed for {self.candidate_id}.",
                file=sys.stderr,
            )
            if log_path:
                print(f"[FIRMARBITER][docker] Build log: {log_path}", file=sys.stderr)

        return success

    def ensure_image(self, build_log_path: str | None = None,
                       build_timeout: int = 1800,
                       stall_timeout: int = 120) -> bool:
        """Build the image if it does not exist yet. Returns True if available."""
        if self.image_exists():
            return True
        return self.build_image(log_path=build_log_path,
                                build_timeout=build_timeout,
                                stall_timeout=stall_timeout)

    def rebuild_image(self, build_log_path: str | None = None) -> bool:
        """Force a full rebuild, pulling the latest tool version."""
        print(f"[FIRMARBITER][docker] Rebuilding {self.candidate_id} from scratch...")
        return self.build_image(log_path=build_log_path, no_cache=True)

    def get_image_commit(self) -> str:
        """
        Return the tool's git commit hash baked into the image.
        This is read from the .firmarbiter_commit file written during the build.
        Returns 'unknown' if not available (e.g. local copy without git).
        """
        try:
            result = subprocess.run(
                ["docker", "run", "--rm", self.image_name,
                 "cat", f"/opt/{self.candidate_id}/.firmarbiter_commit"],
                capture_output=True, text=True, timeout=15,
            )
            commit = result.stdout.strip()
            return commit if commit else "unknown"
        except Exception:
            return "unknown"

    # ── Container lifecycle ───────────────────────────────────────────────────

    def _start_container(self, firmware_path: str, output_dir: str,
                          architecture: str) -> str | None:
        """Start a container and return its ID, or None on failure."""
        firmware_dir  = str(Path(firmware_path).parent)
        firmware_file = Path(firmware_path).name

        # Pass all loop devices and device mapper from host to container
        # FirmAE needs loop devices AND /dev/mapper for kpartx to work
        import glob as _glob
        loop_flags = []
        if Path("/dev/loop-control").exists():
            loop_flags += ["--device", "/dev/loop-control"]
        for _ld in sorted(_glob.glob("/dev/loop[0-9]*")):
            loop_flags += ["--device", _ld]
        # Device mapper — needed for kpartx to create partition devices
        if Path("/dev/mapper/control").exists():
            loop_flags += ["--device", "/dev/mapper/control"]
        for _dm in sorted(_glob.glob("/dev/mapper/*")):
            if not _dm.endswith("control"):
                loop_flags += ["--device", _dm]
        # Also pass /dev/dm-* devices
        for _dm in sorted(_glob.glob("/dev/dm-*")):
            loop_flags += ["--device", _dm]

        cmd = [
            "docker", "run",
            "--detach",
            "--rm",
            "--privileged",
            "--network", "host",
        ] + loop_flags + [
            "--volume", f"{firmware_dir}:{CONTAINER_FIRMWARE_DIR}:ro",
            "--volume", f"{output_dir}:{CONTAINER_OUTPUT_DIR}:rw",
            "--env", f"FIRMARBITER_FIRMWARE={CONTAINER_FIRMWARE_DIR}/{firmware_file}",
            "--env", f"FIRMARBITER_ARCH={architecture}",
            "--env", f"FIRMARBITER_OUTPUT={CONTAINER_OUTPUT_DIR}",
            self.image_name,
            f"{CONTAINER_FIRMWARE_DIR}/{firmware_file}",
            architecture,
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=CONTAINER_START_TIMEOUT,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            print(
                f"[FIRMARBITER][docker] ERROR starting container: {result.stderr}",
                file=sys.stderr,
            )
            return None
        except subprocess.TimeoutExpired:
            print(
                f"[FIRMARBITER][docker] ERROR: Container did not start within "
                f"{CONTAINER_START_TIMEOUT}s",
                file=sys.stderr,
            )
            return None

    def _stop_container(self, container_id: str):
        """Gracefully stop a container, force-kill if needed."""
        try:
            subprocess.run(
                ["docker", "stop", "--time", "10", container_id],
                capture_output=True, timeout=30,
            )
        except Exception:
            try:
                subprocess.run(
                    ["docker", "kill", container_id],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass

    def _stream_logs(self, container_id: str, stdout_log_path: str,
                      stop_event: threading.Event):
        """Stream container logs to a file in a background thread."""
        try:
            proc = subprocess.Popen(
                ["docker", "logs", "--follow", container_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            with open(stdout_log_path, "w") as fh:
                for line in proc.stdout:
                    fh.write(line)
                    fh.flush()
                    if stop_event.is_set():
                        proc.terminate()
                        break
        except Exception:
            pass

    @contextmanager
    def run_container(self, firmware_path: str, architecture: str,
                       output_dir: str, stdout_log_path: str | None = None):
        """
        Context manager: start a fresh container, yield its ID, destroy it on exit.

        Usage:
            with runner.run_container(fw, arch, out_dir) as container_id:
                # container is running
            # container is destroyed here
        """
        container_id    = self._start_container(firmware_path, output_dir, architecture)
        log_stop_event  = threading.Event()
        log_thread      = None

        if container_id and stdout_log_path:
            log_thread = threading.Thread(
                target=self._stream_logs,
                args=(container_id, stdout_log_path, log_stop_event),
                daemon=True,
            )
            log_thread.start()

        try:
            yield container_id
        finally:
            log_stop_event.set()
            if container_id:
                self._stop_container(container_id)
            if log_thread:
                log_thread.join(timeout=5)

    def is_container_running(self, container_id: str) -> bool:
        """Return True if the container is still running."""
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format",
                 "{{.State.Running}}", container_id],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip().lower() == "true"
        except Exception:
            return False

    def get_container_logs(self, container_id: str) -> str:
        """Return the current stdout/stderr output of a running container."""
        try:
            result = subprocess.run(
                ["docker", "logs", container_id],
                capture_output=True, text=True, timeout=10,
            )
            return result.stdout + result.stderr
        except Exception:
            return ""
