"""
firmarbiter_selfcheck.py
--------------------
Runs automatically every time FIRMARBITER starts. Checks that all Python
dependencies are installed and up to date, verifies that required system
tools are present, and warns about anything that could cause a run to fail
before the run actually starts.

This module exists because one of the core criticisms FIRMARBITER makes of other
firmware tools is that they have undocumented or outdated dependencies that
break silently. FIRMARBITER holds itself to the same standard it applies to others.

The check is designed to be fast (under 5 seconds normally) and non-blocking.
If a dependency is outdated it upgrades automatically. If a system tool is
missing it tells you exactly what to install and exits cleanly rather than
crashing mid-run.

This module is imported and called by run_firmarbiter.py at startup. You can
also run it directly to check your environment without starting a benchmark:

    python firmarbiter_selfcheck.py
"""

import importlib
import shutil
import subprocess
import sys
from pathlib import Path


# ── Python package requirements ───────────────────────────────────────────────
#
# Each entry is (import_name, pip_name, minimum_version_tuple).
# import_name   — what you use in `import X`
# pip_name      — what pip knows the package as
# min_version   — the oldest version we are confident works correctly
#
# When you add a new import to any FIRMARBITER module, add a corresponding
# entry here so the self-check catches it.

REQUIRED_PACKAGES = [
    ("requests",           "requests",           (2, 28, 0)),
    ("jsonschema",         "jsonschema",          (4, 17, 0)),
    ("pandas",             "pandas",              (1,  5, 0)),
    ("psutil",             "psutil",              (5,  9, 0)),
    ("pythonjsonlogger",   "python-json-logger",  (2,  0, 4)),
    ("packaging",          "packaging",           (21, 0, 0)),
]

# ── System tool requirements ──────────────────────────────────────────────────
#
# Each entry is (command, install_hint).
# These are tools FIRMARBITER calls via subprocess — if they are missing,
# certain probes will silently fail without this check.

# Docker is checked separately below with a detailed message because
# it is central to FIRMARBITER's isolation model, not just an optional tool.
REQUIRED_SYSTEM_TOOLS = [
    ("binwalk",  "sudo apt install binwalk"),
    ("ip",       "sudo apt install iproute2"),
    ("pgrep",    "sudo apt install procps"),
    ("curl",     "sudo apt install curl"),
]

# System tools that are useful but not strictly required — we warn but
# do not exit if these are missing.
OPTIONAL_SYSTEM_TOOLS = [
    ("git",      "sudo apt install git"),
]


# ── Version helpers ───────────────────────────────────────────────────────────

def _parse_version(version_str: str) -> tuple:
    """
    Turn a version string like '2.31.0' into a comparable tuple (2, 31, 0).
    Handles messy strings like '2.31.0.post1' by keeping only the numeric parts.
    """
    from packaging.version import Version
    try:
        v = Version(version_str)
        return (v.major, v.minor, v.micro)
    except Exception:
        # Fall back to simple splitting if packaging can't handle it
        parts = []
        for segment in version_str.split("."):
            numeric = "".join(c for c in segment if c.isdigit())
            if numeric:
                parts.append(int(numeric))
        return tuple(parts) if parts else (0,)


def _installed_version(import_name: str) -> tuple | None:
    """
    Return the installed version of a package as a tuple, or None if
    the package is not installed or has no __version__ attribute.
    """
    try:
        module = importlib.import_module(import_name)
        version_str = getattr(module, "__version__", None)
        if version_str:
            return _parse_version(version_str)
    except ImportError:
        pass
    # Some packages do not set __version__ — try importlib.metadata
    try:
        from importlib.metadata import version, PackageNotFoundError
        version_str = version(import_name)
        return _parse_version(version_str)
    except Exception:
        pass
    return None


def _pip_cmd() -> list:
    """
    Return the pip command to use for installing packages.

    On Ubuntu 24.04+ the system Python is externally managed (PEP 668)
    and rejects direct pip installs. FIRMARBITER always runs inside its own
    virtual environment created by setup.sh, so sys.executable already
    points at the venv Python. We use that directly.

    If somehow the venv does not exist yet (e.g. selfcheck is called
    before setup.sh has run), we fall back to --break-system-packages
    so at least a helpful error appears rather than a silent failure.
    """
    venv_pip = Path(sys.executable).parent / "pip"
    if venv_pip.exists():
        return [str(venv_pip)]
    # Fallback: use the current Python's -m pip with the system override.
    # This should only happen if someone runs selfcheck outside the venv.
    return [sys.executable, "-m", "pip", "--break-system-packages"]


def _upgrade_package(pip_name: str) -> bool:
    """Upgrade a package. Returns True if successful."""
    try:
        result = subprocess.run(
            _pip_cmd() + ["install", "--upgrade", "--quiet", pip_name],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _install_package(pip_name: str) -> bool:
    """Install a missing package. Returns True if successful."""
    try:
        result = subprocess.run(
            _pip_cmd() + ["install", "--quiet", pip_name],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


# ── Main check functions ──────────────────────────────────────────────────────

def check_running_in_venv() -> bool:
    """
    Warn if FIRMARBITER is not running inside its own virtual environment.
    This is not a fatal error but it means dependencies may not be
    isolated from the system Python and PEP 668 errors may occur.
    """
    # sys.prefix != sys.base_prefix means we are inside a venv
    in_venv = sys.prefix != sys.base_prefix
    if not in_venv:
        print("[FIRMARBITER] Warning: not running inside the FIRMARBITER virtual environment.")
        print("[FIRMARBITER]          Run setup.sh first, then use ./python instead of python3.")
        print("[FIRMARBITER]          Commands:")
        print("[FIRMARBITER]            bash setup.sh")
        print("[FIRMARBITER]            ./python run_firmarbiter.py --list-candidates")
    return in_venv


def check_python_version() -> bool:
    """Verify Python is at least 3.10."""
    major, minor = sys.version_info.major, sys.version_info.minor
    if major < 3 or (major == 3 and minor < 10):
        print(f"[FIRMARBITER] ERROR: Python 3.10 or higher is required.")
        print(f"[FIRMARBITER]        You are running Python {major}.{minor}.")
        print(f"[FIRMARBITER]        Install a newer Python: sudo apt install python3.12")
        return False
    return True


def check_and_update_packages(auto_update: bool = True) -> bool:
    """
    Check all required Python packages. Install missing ones and upgrade
    outdated ones automatically if auto_update is True.

    Returns True if all packages are available after the check.
    """
    all_ok = True

    for import_name, pip_name, min_version in REQUIRED_PACKAGES:
        installed = _installed_version(import_name)

        if installed is None:
            # Package is not installed at all
            print(f"[FIRMARBITER] Missing package: {pip_name} — installing...")
            if auto_update and _install_package(pip_name):
                print(f"[FIRMARBITER]   Installed {pip_name} successfully.")
            else:
                print(f"[FIRMARBITER]   Could not install {pip_name} automatically.")
                print(f"[FIRMARBITER]   Run manually: pip install {pip_name}")
                all_ok = False
            continue

        if installed < min_version:
            # Package is installed but too old
            installed_str = ".".join(str(x) for x in installed)
            required_str  = ".".join(str(x) for x in min_version)
            print(f"[FIRMARBITER] Outdated: {pip_name} "
                  f"(installed {installed_str}, need >={required_str}) — upgrading...")
            if auto_update and _upgrade_package(pip_name):
                print(f"[FIRMARBITER]   Upgraded {pip_name} successfully.")
            else:
                print(f"[FIRMARBITER]   Could not upgrade {pip_name} automatically.")
                print(f"[FIRMARBITER]   Run manually: pip install --upgrade {pip_name}")
                all_ok = False

    return all_ok


def check_system_tools() -> bool:
    """
    Verify required system tools are on PATH.
    Exits with a clear message for each missing tool rather than letting
    a probe fail silently later.
    """
    all_ok = True

    for tool, hint in REQUIRED_SYSTEM_TOOLS:
        if shutil.which(tool) is None:
            print(f"[FIRMARBITER] Missing system tool: {tool}")
            print(f"[FIRMARBITER]   Install with: {hint}")
            all_ok = False

    for tool, hint in OPTIONAL_SYSTEM_TOOLS:
        if shutil.which(tool) is None:
            print(f"[FIRMARBITER] Optional tool not found: {tool} "
                  f"(some features may be limited)")

    return all_ok


def check_docker() -> bool:
    """
    Check that Docker is installed and the daemon is running.
    Docker is required for candidate isolation — without it, tool
    dependencies contaminate each other and the benchmark findings
    are not reliable.
    """
    import shutil
    if shutil.which("docker") is None:
        print("[FIRMARBITER] Docker is not installed.")
        print("[FIRMARBITER] Docker is required for candidate isolation.")
        print("[FIRMARBITER] Install it from: https://docs.docker.com/engine/install/ubuntu/")
        return False

    # Check whether the Docker daemon is actually running
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            print("[FIRMARBITER] Docker is installed but the daemon is not running.")
            print("[FIRMARBITER] Start it with: sudo systemctl start docker")
            print("[FIRMARBITER] Enable on boot: sudo systemctl enable docker")
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print("[FIRMARBITER] Could not connect to Docker daemon.")
        return False

    return True


def check_schema_files() -> bool:
    """Verify the result schema file exists."""
    schema = Path("schemas/result_schema.json")
    if not schema.exists():
        print(f"[FIRMARBITER] Warning: schemas/result_schema.json not found. "
              f"Result validation will be skipped.")
        return False
    return True


def run_selfcheck(auto_update: bool = True, silent_if_ok: bool = True) -> bool:
    """
    Run all self-checks. Called automatically by run_firmarbiter.py at startup.

    Parameters
    ----------
    auto_update : bool
        If True, automatically install or upgrade outdated packages.
        Set to False to check-only without making changes.
    silent_if_ok : bool
        If True, print nothing when everything is fine. Only print when
        something needs attention. This keeps normal runs uncluttered.

    Returns
    -------
    bool
        True if the environment is ready to run. False if something critical
        is missing and the run should not proceed.
    """
    check_running_in_venv()  # warning only, does not block
    python_ok  = check_python_version()
    if not python_ok:
        return False

    packages_ok = check_and_update_packages(auto_update=auto_update)
    tools_ok    = check_system_tools()
    docker_ok   = check_docker()
    check_schema_files()  # warning only, does not block

    all_ok = packages_ok and tools_ok and docker_ok

    if all_ok and silent_if_ok:
        pass  # Everything is fine — say nothing
    elif all_ok:
        print("[FIRMARBITER] Self-check passed. Environment is ready.")

    return all_ok


# ── Standalone usage ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n[FIRMARBITER] Running environment self-check...\n")

    ok = run_selfcheck(auto_update=True, silent_if_ok=False)

    if ok:
        print("\n[FIRMARBITER] All checks passed. FIRMARBITER is ready to run.")
    else:
        print("\n[FIRMARBITER] Some checks failed. Fix the issues above before running.")
        sys.exit(1)
