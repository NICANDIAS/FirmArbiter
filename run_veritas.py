#!/usr/bin/env python3
"""
run_veritas.py
--------------
Main entry point for VERITAS. Scans a folder of firmware images,
discovers available candidate tools from the candidates/ directory,
and runs the full benchmark automatically.

Neither the firmware folder nor the candidate tools need to be
pre-registered anywhere. VERITAS discovers both at runtime.

Typical usage:

    # Run all discovered candidates against all firmware in a folder
    python run_veritas.py --firmware /path/to/firmware_folder

    # Run one specific candidate
    python run_veritas.py --firmware /path/to/firmware_folder --candidates firmae

    # Run one specific firmware file through one candidate (for debugging)
    python run_veritas.py --firmware /path/to/DIR-868L.zip --candidates firmae

    # See what VERITAS found without running anything
    python run_veritas.py --firmware /path/to/firmware_folder --dry-run

    # List all discovered candidates
    python run_veritas.py --list-candidates
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# ── Config loader ─────────────────────────────────────────────────────────────

def load_config(config_path: str = "veritas.conf") -> dict:
    """
    Read veritas.conf and return a dict of key/value pairs.
    Also exports each value as an environment variable so the adapter
    shell scripts can read FIRMAE_DIR, FIRMADYNE_DIR, etc. directly
    without any extra wiring.
    """
    config = {}
    conf_file = Path(config_path)
    if not conf_file.exists():
        return config
    with open(conf_file) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip()
            config[key] = value
            os.environ[key] = value
    return config


_CONFIG = load_config()

# Run the self-check at import time. This installs or upgrades any outdated
# Python packages automatically before anything else runs. System tool checks
# run silently — they only print if something is missing.
from veritas_selfcheck import run_selfcheck as _selfcheck
_selfcheck(auto_update=True, silent_if_ok=True)

# Registration module — used when --register-tool is passed
from veritas_register import register_tool

# ── Probe imports ─────────────────────────────────────────────────────────────

from probes.probe_unpack    import probe_unpack
from probes.probe_boot      import probe_boot
from probes.probe_service   import probe_service
from probes.probe_stability import probe_stability
from probes.probe_cleanup   import probe_cleanup


# ── Constants (read from config, fall back to defaults) ───────────────────────

DEFAULT_TIMEOUT        = int(_CONFIG.get("TIMEOUT_SECONDS",             300))
BUILD_TIMEOUT          = int(_CONFIG.get("BUILD_TIMEOUT_SECONDS",       1800))
BUILD_STALL_TIMEOUT    = int(_CONFIG.get("BUILD_STALL_TIMEOUT_SECONDS",  300))
STALL_TIMEOUT          = int(_CONFIG.get("STALL_TIMEOUT_SECONDS",         600))

# Make stall timeout available to probe_boot.py via environment
import os as _os
_os.environ["STALL_TIMEOUT_SECONDS"] = str(STALL_TIMEOUT)
RESULTS_DIR      = Path(_CONFIG.get("RESULTS_DIR",      "results/runs"))
LOGS_DIR         = Path(_CONFIG.get("LOGS_DIR",         "results/logs"))
UNPACK_CACHE     = Path(_CONFIG.get("UNPACK_CACHE_DIR", "results/unpack_cache"))
ZIP_EXTRACT_DIR  = Path(_CONFIG.get("ZIP_EXTRACT_DIR",  "results/zip_extracted"))
CANDIDATES_DIR   = Path("candidates")

# File extensions VERITAS treats as firmware images when scanning a folder.
# Add more here if you encounter other packaging formats in your corpus.
FIRMWARE_EXTENSIONS = {".bin", ".zip", ".img", ".tar", ".gz", ".bz2",
                       ".7z", ".trx", ".chk", ".dlf", ".w", ".blob"}


def _reset_firmae_state_in_containers():
    """
    Reset FirmAE's accumulated database and scratch state.
    Called once at the start of each VERITAS session, not between images.

    This is necessary because FirmAE uses a persistent PostgreSQL database
    that accumulates IID entries across runs. Without resetting, subsequent
    runs on different firmware images get IID collisions.

    This is documented as Finding F13 — FirmAE has no built-in run isolation.
    The database persists across runs, meaning each run is not independent.
    """
    import subprocess
    from docker_runner import DockerRunner
    from pathlib import Path

    print("[VERITAS] Resetting FirmAE session state (Finding F13)...")

    # Clean up stale loop devices left by previous FirmAE runs (Finding F7)
    # FirmAE does not detach loop devices on container exit, causing
    # subsequent runs to fail with "failed to set up loop device"
    import subprocess as _sp
    try:
        lo_result = _sp.run(
            ["sudo", "losetup", "-l"],
            capture_output=True, text=True, timeout=10
        )
        stale = []
        for line in lo_result.stdout.splitlines():
            if "firmae" in line.lower() or "(deleted)" in line:
                dev = line.split()[0]
                if dev.startswith("/dev/loop"):
                    stale.append(dev)
        if stale:
            print(f"[VERITAS] Detaching {len(stale)} stale loop devices "
                  f"(Finding F7)...")
            for dev in stale:
                _sp.run(["sudo", "losetup", "-d", dev],
                        capture_output=True, timeout=5)
            print(f"[VERITAS] Stale loop devices cleared: {stale}")
        else:
            print("[VERITAS] No stale loop devices found")
    except Exception as e:
        print(f"[VERITAS] Loop device cleanup skipped: {e}")

    firmae_candidate = Path("candidates/firmae")
    if not firmae_candidate.exists():
        return

    runner = DockerRunner(firmae_candidate)
    if not runner.image_exists():
        return  # Image not built yet, nothing to reset

    try:
        # Run a quick reset inside a temporary FirmAE container
        result = subprocess.run(
            [
                "docker", "run", "--rm", "--privileged",
                "--network", "host",
                "--entrypoint", "bash",
                "veritas/firmae",
                "-c",
                "service postgresql start 2>/dev/null || true; "
                "sleep 2; "
                "sudo -u postgres psql -d firmware "
                "-c 'DELETE FROM image;' 2>/dev/null || true; "
                "rm -rf /opt/firmae/scratch/* 2>/dev/null || true; "
                "rm -rf /opt/firmae/images/* 2>/dev/null || true; "
                "echo '[VERITAS] FirmAE state reset complete'"
            ],
            capture_output=False,
            timeout=30,
        )
        if result.returncode == 0:
            print("[VERITAS] FirmAE state reset complete")
        else:
            print("[VERITAS] FirmAE state reset skipped (non-fatal)")
    except Exception as e:
        print(f"[VERITAS] FirmAE state reset skipped: {e}")


# ── Session-level build failure cache ────────────────────────────────────────
# If a Docker image fails to build, we record it here and skip all subsequent
# runs for that candidate in this session rather than retrying the failed build
# for every firmware image (which wastes time and floods the log with the same
# error repeatedly).
_BUILD_FAILED: set = set()

# ── Candidate discovery ───────────────────────────────────────────────────────

def parse_candidate_conf(conf_path: Path) -> dict:
    """
    Read a candidate.conf file and return a dict of its settings.
    Returns an empty dict if the file cannot be parsed.
    """
    settings = {}
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


def discover_candidates() -> dict:
    """
    Scan the candidates/ folder and return a dict of all valid candidates.

    A valid candidate is a subfolder of candidates/ that contains:
        - run_adapter.sh   (required — the tool invocation script)
        - candidate.conf   (required — tool metadata and success signals)

    The declared_deps.txt and actual_deps.txt files are optional but
    recommended for reproducibility documentation.

    Returns a dict keyed by candidate ID:
        {
          "firmae": {
              "id": "firmae",
              "name": "FirmAE",
              "adapter": Path("candidates/firmae/run_adapter.sh"),
              "success_signals": ["Network reachable", "Web service"],
              "supported_architectures": ["arm", "mips", ...],
              "requires_sudo": True,
              "version_hint": "https://github.com/...",
          },
          ...
        }
    """
    candidates = {}

    if not CANDIDATES_DIR.exists():
        return candidates

    for subdir in sorted(CANDIDATES_DIR.iterdir()):
        if not subdir.is_dir():
            continue

        adapter = subdir / "run_adapter.sh"
        conf    = subdir / "candidate.conf"

        if not adapter.exists():
            continue  # Not a candidate folder — skip silently

        if not conf.exists():
            print(f"[VERITAS] Warning: {subdir.name}/ has run_adapter.sh but no "
                  f"candidate.conf — skipping. Create candidate.conf to register "
                  f"this tool.", file=sys.stderr)
            continue

        settings = parse_candidate_conf(conf)
        candidate_id = settings.get("id", subdir.name)

        # Parse the comma-separated success_signals into a list
        raw_signals = settings.get("success_signals", "")
        signals = [s.strip() for s in raw_signals.split(",") if s.strip()]

        # Parse supported architectures
        raw_archs = settings.get("supported_architectures", "arm,mips")
        archs = [a.strip() for a in raw_archs.split(",") if a.strip()]

        candidates[candidate_id] = {
            "id":                      candidate_id,
            "name":                    settings.get("name", subdir.name),
            "adapter":                 adapter,
            "success_signals":         signals,
            "supported_architectures": archs,
            "requires_sudo":           settings.get("requires_sudo", "false").lower() == "true",
            "version_hint":            settings.get("version_hint", "unknown"),
            "conf_dir":                subdir,
        }

    return candidates


# ── Firmware folder scanning ──────────────────────────────────────────────────

def sha256_of_file(path: str) -> str:
    """Compute SHA256 of a file in chunks to handle large images."""
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def is_likely_encrypted(fpath: Path, binwalk_output: str = "") -> bool:
    """
    Return True if the firmware is likely encrypted and therefore out of scope.
    Detection uses two signals:
      1. Filename contains known encryption markers
      2. Binwalk found no known filesystem or compression signatures
         but the file is large enough to be a real firmware image
    """
    lower = fpath.name.lower()
    # Filename-based detection
    encryption_markers = ["encrypt", "_enc", "-enc", "crypt", "_cry", "secure"]
    if any(marker in lower for marker in encryption_markers):
        return True
    # Binwalk-based detection: if binwalk ran but found nothing useful
    if binwalk_output:
        lower_bw = binwalk_output.lower()
        has_useful_content = any(x in lower_bw for x in [
            "squashfs", "jffs2", "cramfs", "ext2", "ext3", "ext4",
            "gzip", "lzma", "xz compressed", "zip archive",
            "elf", "u-boot", "linux kernel",
        ])
        if not has_useful_content and fpath.stat().st_size > 512_000:
            return True
    return False


def prepare_firmware_path(fpath: Path, work_dir: Path) -> tuple:
    """
    Prepare the firmware for analysis. If it is a ZIP file, extract the
    largest binary file from inside it and return that path instead.
    Also detect encryption and flag accordingly.

    Returns (prepared_path, was_extracted, encryption_suspected, notes)
    """
    import zipfile
    import tempfile

    notes = []
    encryption_suspected = False

    # Check filename-based encryption signal before doing anything else
    if is_likely_encrypted(fpath):
        encryption_suspected = True
        notes.append(f"encryption_suspected_from_filename: {fpath.name}")

    # If not a zip, return as-is
    if fpath.suffix.lower() not in (".zip",):
        return str(fpath), False, encryption_suspected, notes

    # Extract from zip
    try:
        with zipfile.ZipFile(str(fpath), "r") as zf:
            # Find the largest file inside — that is almost certainly the firmware
            members = [m for m in zf.infolist()
                       if not m.filename.endswith("/") and m.file_size > 512]
            if not members:
                notes.append("zip_empty_or_only_small_files")
                return str(fpath), False, encryption_suspected, notes

            # Sort by size descending — the firmware binary is the biggest file
            members.sort(key=lambda m: m.file_size, reverse=True)
            target = members[0]

            # Extract to a clean path under work_dir
            work_dir.mkdir(parents=True, exist_ok=True)
            clean_name = Path(target.filename).name
            clean_name = "".join(c if c.isalnum() or c in "-_." else "_"
                                 for c in clean_name)
            out_path = work_dir / clean_name

            if not out_path.exists():
                with zf.open(target) as src, open(out_path, "wb") as dst:
                    dst.write(src.read())

            notes.append(f"extracted_from_zip: {target.filename} "
                         f"({target.file_size:,} bytes)")

            # Re-check encryption on the extracted file
            if is_likely_encrypted(out_path):
                encryption_suspected = True
                notes.append(f"encryption_suspected_from_extracted_filename")

            return str(out_path), True, encryption_suspected, notes

    except zipfile.BadZipFile:
        notes.append("bad_zip_file")
        return str(fpath), False, encryption_suspected, notes
    except Exception as exc:
        notes.append(f"zip_extraction_error: {exc}")
        return str(fpath), False, encryption_suspected, notes


def guess_architecture(filename: str) -> str:
    """
    Make a best-effort guess at the CPU architecture from the filename.

    Handles both direct architecture names (arm, mips) and OpenWrt-style
    target/subtarget strings like:
        ath79         → mips
        ramips        → mips
        mvebu         → arm
        bcm27xx       → arm
        ipq806x       → arm
        x86           → x86
        cortexa9/a53  → arm

    This is a hint only — tools receive it as a starting point and may
    override it with their own detection. Returns 'unknown' if nothing matches.
    """
    lower = filename.lower()

    # ── Explicit architecture strings ─────────────────────────────────────────
    # Check these first, most-specific to least-specific
    if any(x in lower for x in ["mipsel", "mips-el", "mips_el", "mipsle",
                                  "ramips", "mt7621", "mt7620", "mt76x8"]):
        return "mipsel"
    if "mips64" in lower:
        return "mips64"
    if any(x in lower for x in ["mips", "ath79", "ath9k", "ar71xx",
                                  "ar7", "ar9"]):
        return "mips"
    if any(x in lower for x in ["aarch64", "arm64", "cortex-a53",
                                  "cortexa53", "bcm2711", "ipq807x",
                                  "ipq60xx"]):
        return "aarch64"
    if any(x in lower for x in ["armeb", "arm-eb"]):
        return "armeb"
    if any(x in lower for x in ["arm", "mvebu", "bcm27xx", "bcm2709",
                                  "bcm2710", "ipq806x", "ipq40xx",
                                  "cortexa9", "cortexa7", "kirkwood",
                                  "orion", "imx6", "sunxi"]):
        return "arm"
    if any(x in lower for x in ["x86_64", "amd64", "x64"]):
        return "x86_64"
    if any(x in lower for x in ["x86", "i386", "i686"]):
        return "x86"
    if any(x in lower for x in ["powerpc", "ppc", "mpc85xx"]):
        return "powerpc"
    return "unknown"


def detect_arch_from_binwalk(firmware_path: str) -> str:
    """
    Detect CPU architecture from firmware binary content using multiple methods.
    This is used when the filename gives no architecture hint.

    Method 1: Binwalk signature scan — looks for known firmware headers
    Method 2: ELF header inspection — reads the machine type directly
    Method 3: String-based heuristics — looks for architecture strings in binary

    Returns the detected architecture string or 'unknown'.
    """
    import shutil
    import subprocess
    import struct

    # Method 1: Try reading ELF header directly if the file is an ELF
    try:
        with open(firmware_path, "rb") as f:
            magic = f.read(4)
            if magic == b"\x7fELF":
                f.seek(18)  # e_machine field
                machine = struct.unpack("<H", f.read(2))[0]
                machine_map = {
                    8:   "mips",
                    40:  "arm",
                    183: "aarch64",
                    3:   "x86",
                    62:  "x86_64",
                    20:  "powerpc",
                    21:  "powerpc",
                }
                if machine in machine_map:
                    return machine_map[machine]
    except Exception:
        pass

    # Method 2: Binwalk signature scan
    binwalk_bin = shutil.which("binwalk") or "/usr/bin/binwalk"
    if binwalk_bin and Path(binwalk_bin).exists():
        try:
            result = subprocess.run(
                [binwalk_bin, "--signature", firmware_path],
                capture_output=True, text=True, timeout=60,
            )
            output = (result.stdout + result.stderr).lower()

            # Check for ELF entries in binwalk output
            if "mipsel" in output or "mips little" in output or                "mips-el" in output:
                return "mipsel"
            if "mips" in output:
                return "mips"
            if "arm64" in output or "aarch64" in output:
                return "aarch64"
            if "arm" in output:
                return "arm"
            if "x86-64" in output or "x86_64" in output:
                return "x86_64"
            if "x86" in output or "80386" in output:
                return "x86"
            if "powerpc" in output or "ppc" in output:
                return "powerpc"
        except Exception:
            pass

    # Method 3: String search in binary for architecture markers
    # Uses frequency counting — the dominant architecture string wins
    try:
        result = subprocess.run(
            ["strings", firmware_path],
            capture_output=True, text=True, timeout=30,
        )
        output = result.stdout

        # Count architecture string occurrences (case-insensitive)
        import re
        lower = output.lower()

        # Definitive compiler/toolchain strings
        if "arm-linux-gnueabi" in lower or "arm-linux-gnu" in lower:
            return "arm"
        if "mipsel-linux" in lower or "mips-linux-gnu" in lower:
            return "mipsel"
        if "mips-linux" in lower or "mips32" in lower:
            return "mips"
        if "aarch64-linux" in lower:
            return "aarch64"
        if "x86_64-linux" in lower:
            return "x86_64"

        # Count raw architecture keyword occurrences
        # Use word boundaries to avoid false matches
        arm_count  = len(re.findall(r'(?i)\barm\b', output))
        mips_count = len(re.findall(r'(?i)\bmips\b', output))
        x86_count  = len(re.findall(r'(?i)\b(?:x86|i386|i686)\b', output))

        counts = {"arm": arm_count, "mips": mips_count, "x86": x86_count}
        best = max(counts, key=counts.get)
        if counts[best] >= 3:  # require at least 3 occurrences to be confident
            return best

    except Exception:
        pass

    # Method 4: Check Binwalk endianness from SquashFS
    # little endian SquashFS on MIPS routers = mipsel
    # little endian SquashFS on ARM routers = arm
    # This is a weak signal so only use as last resort
    try:
        binwalk_bin = shutil.which("binwalk") or "/usr/bin/binwalk"
        if binwalk_bin and Path(binwalk_bin).exists():
            result = subprocess.run(
                [binwalk_bin, "--signature", firmware_path],
                capture_output=True, text=True, timeout=60,
            )
            output = result.stdout.lower()
            if "squashfs filesystem, little endian" in output:
                # Cannot determine arm vs mipsel from endianness alone
                # Return unknown and let the tool decide
                pass
    except Exception:
        pass

    return "unknown"


def detect_arch_from_binwalk(firmware_path: str) -> str:
    """
    Detect CPU architecture from firmware binary content using multiple methods.
    This is used when the filename gives no architecture hint.

    Method 1: Binwalk signature scan — looks for known firmware headers
    Method 2: ELF header inspection — reads the machine type directly
    Method 3: String-based heuristics — looks for architecture strings in binary

    Returns the detected architecture string or 'unknown'.
    """
    import shutil
    import subprocess
    import struct

    # Method 1: Try reading ELF header directly if the file is an ELF
    try:
        with open(firmware_path, "rb") as f:
            magic = f.read(4)
            if magic == b"\x7fELF":
                f.seek(18)  # e_machine field
                machine = struct.unpack("<H", f.read(2))[0]
                machine_map = {
                    8:   "mips",
                    40:  "arm",
                    183: "aarch64",
                    3:   "x86",
                    62:  "x86_64",
                    20:  "powerpc",
                    21:  "powerpc",
                }
                if machine in machine_map:
                    return machine_map[machine]
    except Exception:
        pass

    # Method 2: Binwalk signature scan
    binwalk_bin = shutil.which("binwalk") or "/usr/bin/binwalk"
    if binwalk_bin and Path(binwalk_bin).exists():
        try:
            result = subprocess.run(
                [binwalk_bin, "--signature", firmware_path],
                capture_output=True, text=True, timeout=60,
            )
            output = (result.stdout + result.stderr).lower()

            # Check for ELF entries in binwalk output
            if "mipsel" in output or "mips little" in output or                "mips-el" in output:
                return "mipsel"
            if "mips" in output:
                return "mips"
            if "arm64" in output or "aarch64" in output:
                return "aarch64"
            if "arm" in output:
                return "arm"
            if "x86-64" in output or "x86_64" in output:
                return "x86_64"
            if "x86" in output or "80386" in output:
                return "x86"
            if "powerpc" in output or "ppc" in output:
                return "powerpc"
        except Exception:
            pass

    # Method 3: String search in binary for architecture markers
    # Uses frequency counting — the dominant architecture string wins
    try:
        result = subprocess.run(
            ["strings", firmware_path],
            capture_output=True, text=True, timeout=30,
        )
        output = result.stdout

        # Count architecture string occurrences (case-insensitive)
        import re
        lower = output.lower()

        # Definitive compiler/toolchain strings
        if "arm-linux-gnueabi" in lower or "arm-linux-gnu" in lower:
            return "arm"
        if "mipsel-linux" in lower or "mips-linux-gnu" in lower:
            return "mipsel"
        if "mips-linux" in lower or "mips32" in lower:
            return "mips"
        if "aarch64-linux" in lower:
            return "aarch64"
        if "x86_64-linux" in lower:
            return "x86_64"

        # Count raw architecture keyword occurrences
        # Use word boundaries to avoid false matches
        arm_count  = len(re.findall(r'(?i)\barm\b', output))
        mips_count = len(re.findall(r'(?i)\bmips\b', output))
        x86_count  = len(re.findall(r'(?i)\b(?:x86|i386|i686)\b', output))

        counts = {"arm": arm_count, "mips": mips_count, "x86": x86_count}
        best = max(counts, key=counts.get)
        if counts[best] >= 3:  # require at least 3 occurrences to be confident
            return best

    except Exception:
        pass

    # Method 4: Check Binwalk endianness from SquashFS
    # little endian SquashFS on MIPS routers = mipsel
    # little endian SquashFS on ARM routers = arm
    # This is a weak signal so only use as last resort
    try:
        binwalk_bin = shutil.which("binwalk") or "/usr/bin/binwalk"
        if binwalk_bin and Path(binwalk_bin).exists():
            result = subprocess.run(
                [binwalk_bin, "--signature", firmware_path],
                capture_output=True, text=True, timeout=60,
            )
            output = result.stdout.lower()
            if "squashfs filesystem, little endian" in output:
                # Cannot determine arm vs mipsel from endianness alone
                # Return unknown and let the tool decide
                pass
    except Exception:
        pass

    return "unknown"


def guess_vendor(filepath: Path) -> str:
    """
    Try to infer the vendor from the folder structure or filename.
    If the firmware lives in a subfolder named after a vendor, use that.
    Otherwise fall back to the filename.
    """
    lower_name = filepath.name.lower()
    lower_parent = filepath.parent.name.lower()

    known_vendors = [
        "dlink", "tplink", "tp-link", "netgear", "asus", "linksys",
        "belkin", "zyxel", "ubiquiti", "mikrotik", "openwrt", "ddwrt",
        "buffalo", "cisco", "huawei", "xiaomi", "digi", "alfa",
    ]
    for vendor in known_vendors:
        if vendor in lower_parent or vendor in lower_name:
            return vendor.replace("-", "")

    # Use parent folder name as vendor if it looks like a vendor name
    # (not a generic name like "firmware" or "images")
    generic_names = {"firmware", "images", "fw", "bin", "downloads", "corpus"}
    if lower_parent not in generic_names:
        return filepath.parent.name

    return "unknown"


def scan_firmware_folder(folder_path: str) -> list:
    """
    Walk folder_path recursively and return a list of firmware case dicts,
    one per firmware file found.

    Each case dict contains everything VERITAS needs to run the benchmark
    against that image — no external manifest required.
    """
    folder = Path(folder_path)

    # If a single file was passed instead of a folder, wrap it in a list
    if folder.is_file():
        if folder.suffix.lower() in FIRMWARE_EXTENSIONS:
            return [_make_case(folder)]
        else:
            print(f"[VERITAS] Warning: {folder} does not look like a firmware file "
                  f"(extension not in {FIRMWARE_EXTENSIONS})", file=sys.stderr)
            return []

    if not folder.exists():
        print(f"[VERITAS] ERROR: Firmware path not found: {folder}", file=sys.stderr)
        sys.exit(1)

    cases = []
    seen_hashes = set()  # skip exact duplicate files

    for fpath in sorted(folder.rglob("*")):
        if not fpath.is_file():
            continue
        if fpath.suffix.lower() not in FIRMWARE_EXTENSIONS:
            continue
        # Skip very small files — likely not real firmware images
        if fpath.stat().st_size < 512:
            continue

        case = _make_case(fpath)

        # Deduplicate by SHA256
        if case["sha256"] in seen_hashes:
            print(f"[VERITAS] Skipping duplicate: {fpath.name}")
            continue
        seen_hashes.add(case["sha256"])

        cases.append(case)

    return cases


def _make_case(fpath: Path) -> dict:
    """Build a single case dict from a firmware file path."""
    sha = sha256_of_file(str(fpath))
    short_hash = sha[:8]
    vendor = guess_vendor(fpath)
    arch = guess_architecture(fpath.name)
    # If filename gave nothing, try detecting from file contents
    if arch == "unknown":
        arch = detect_arch_from_binwalk(str(fpath))

    # case_id format: vendor__filename_stem__short_hash
    # Sanitise the filename to make a clean ID
    stem = fpath.stem.lower()
    stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in stem)
    stem = stem.strip("_.")[:40]  # cap at 40 chars

    case_id = f"{vendor}__{stem}__{short_hash}"

    return {
        "case_id":       case_id,
        "firmware_path": str(fpath),
        "filename":      fpath.name,
        "vendor":        vendor,
        "architecture":  arch,
        "sha256":        sha,
        "short_hash":    short_hash,
        "file_size_bytes": fpath.stat().st_size,
    }


# ── Run helpers ───────────────────────────────────────────────────────────────

def make_run_id(case_id: str, candidate_id: str) -> str:
    """
    Build a run id that is still readable but avoids collisions between
    firmware files in the same vendor/folder.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    safe_case = "".join(c if c.isalnum() or c in "-_." else "_" for c in case_id)
    return f"{ts}__{candidate_id}__{safe_case}"


def already_completed(case_id: str, candidate_id: str) -> bool:
    """
    Return True only if an existing JSON result has the exact same case_id
    and candidate/tool id. Do not rely on truncated filenames, because many
    cases can share the same prefix, e.g. vendor_firmware__...
    """
    if not RESULTS_DIR.exists():
        return False

    for path in RESULTS_DIR.glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue

        if data.get("case_id") == case_id and data.get("tool") == candidate_id:
            return True

    return False


def write_result(result: dict, run_id: str):
    """Write result dict to results/runs/<run_id>.json."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"{run_id}.json"

    # Validate against schema if available
    schema_path = Path("schemas/result_schema.json")
    if schema_path.exists():
        try:
            import jsonschema
            with open(schema_path) as fh:
                schema = json.load(fh)
            jsonschema.validate(result, schema)
        except Exception as exc:
            print(f"[VERITAS] Warning: schema validation failed: {exc}",
                  file=sys.stderr)

    with open(out_path, "w") as fh:
        json.dump(result, fh, indent=2)

    print(f"[VERITAS] Result → {out_path}")


# ── Core run logic ─────────────────────────────────────────────────────────────

def run_one(case: dict, candidate: dict, timeout: int, dry_run: bool = False) -> dict | None:
    """
    Run one candidate tool against one firmware image.
    Orchestrates all five probes and assembles the result.
    """
    case_id        = case["case_id"]
    firmware_path  = case["firmware_path"]
    architecture   = case["architecture"]
    candidate_id   = candidate["id"]
    candidate_name = candidate["name"]

    # Skip if the architecture is not supported by this candidate
    supported = candidate["supported_architectures"]
    if architecture != "unknown" and supported and architecture not in supported:
        print(f"[VERITAS] Skipping {case_id} / {candidate_id}: "
              f"architecture '{architecture}' not in {supported}")
        return None

    # Skip if already done
    if already_completed(case_id, candidate_id):
        print(f"[VERITAS] Skipping {case_id} / {candidate_id}: already completed")
        return None

    run_id = make_run_id(case_id, candidate_id)

    # ── Prepare firmware — extract from zip, detect encryption ───────────────
    prepared_path, was_extracted, encryption_suspected, prep_notes = \
        prepare_firmware_path(Path(firmware_path), (ZIP_EXTRACT_DIR / case_id[:16]).resolve())

    if encryption_suspected:
        print(f"[VERITAS] Suspected encrypted firmware: {case['filename']}")
        print(f"[VERITAS] Recording as out_of_scope — skipping tool runs.")
        oos_result = {
            "schema_version": "1.0",
            "case_id": case_id,
            "firmware_filename": case["filename"],
            "vendor": case["vendor"],
            "tool": candidate_id,
            "tool_name": candidate["name"],
            "tool_version": "n/a",
            "run_id": run_id,
            "run_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "architecture": architecture,
            "firmware_sha256": case["sha256"],
            "out_of_scope": True,
            "out_of_scope_reason": "encryption_suspected",
            "unpack": {"veritas_rootfs_found": False, "veritas_fs_types": [],
                       "veritas_elf_count": 0, "tool_claimed_unpack": None},
            "boot": {"success": False, "wall_time_seconds": None,
                     "cpu_seconds": None, "peak_ram_mb": None,
                     "timeout_seconds": timeout, "timed_out": False,
                     "failure_reason": "out_of_scope_encrypted"},
            "service": {"reported_ip": None, "reported_port": 80,
                        "tcp_connect": None, "http_status": None,
                        "body_size_bytes": None, "body_sha256": None,
                        "service_reachable": False, "service_authentic": False,
                        "false_positive_reason": None},
            "stability": {"probe_attempted": False,
                          "service_reachable_at_60s": None,
                          "service_authentic_at_60s": None},
            "cleanup": {"stale_tap_devices": [], "orphan_qemu_pids": [],
                        "orphan_docker_containers": [], "cleanup_clean": True},
            "logs": {"stdout_path": None, "stderr_path": None},
            "notes": prep_notes,
        }
        write_result(oos_result, run_id)
        return oos_result

    if was_extracted:
        print(f"[VERITAS] Extracted from zip → {Path(prepared_path).name}")
    firmware_path = prepared_path

    if dry_run:
        print(f"[VERITAS][DRY RUN] {candidate_name} ← {case['filename']} "
              f"(arch={architecture}, timeout={timeout}s)")
        return None

    print(f"\n{'='*62}")
    print(f"[VERITAS] {candidate_name}  ←  {case['filename']}")
    print(f"[VERITAS] case_id={case_id}  arch={architecture}  timeout={timeout}s")
    print(f"{'='*62}")

    run_timestamp  = datetime.now(timezone.utc).isoformat()
    stdout_log     = str(LOGS_DIR / f"{run_id}__stdout.txt")
    stderr_log     = str(LOGS_DIR / f"{run_id}__stderr.txt")
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. Independent unpack probe ───────────────────────────────────────────
    print(f"[VERITAS] 1/5  unpack probe ...")
    unpack_result = probe_unpack(
        firmware_path=firmware_path,
        firmware_sha256=case["sha256"],
        cache_dir=str(UNPACK_CACHE),
    )
    _ctypes = unpack_result.get("veritas_content_types", unpack_result.get("veritas_fs_types", []))
    print(f"         rootfs={unpack_result['veritas_rootfs_found']}  "
          f"content={_ctypes}  "
          f"elfs={unpack_result['veritas_elf_count']}")

    # ── 2. Tool invocation + boot monitoring ──────────────────────────────────
    print(f"[VERITAS] 2/5  running {candidate_name} ...")

    # Skip if this candidate's image already failed to build this session
    if candidate_id in _BUILD_FAILED:
        print(f"[VERITAS] Skipping {candidate_id}: image build failed earlier "
              f"this session. Fix the network and use --rebuild-image {candidate_id}")
        boot_result = {
            "success": False, "wall_time_seconds": None,
            "cpu_seconds": None, "peak_ram_mb": None,
            "timeout_seconds": timeout, "timed_out": False,
            "failure_reason": "image_build_failed_earlier_this_session",
            "tool_version": "unknown",
        }
        reported_ip = None
    else:
        boot_result_raw = None
        try:
            boot_result_raw = probe_boot(
                candidate_dir=candidate["conf_dir"],
                firmware_path=firmware_path,
                architecture=architecture,
                success_signals=candidate["success_signals"],
                timeout_seconds=timeout,
                stdout_log_path=stdout_log,
                stderr_log_path=stderr_log,
                output_dir=str((RESULTS_DIR.parent / "container_output" / run_id).resolve()),
                build_timeout=BUILD_TIMEOUT,
                build_stall_timeout=BUILD_STALL_TIMEOUT,
            )
        except Exception as exc:
            boot_result_raw = {"failure_reason": str(exc), "success": False,
                               "wall_time_seconds": None, "cpu_seconds": None,
                               "peak_ram_mb": None, "timeout_seconds": timeout,
                               "timed_out": False, "tool_version": "unknown"}

        # If the image failed to build, record it so we skip this tool
        # for all remaining firmware images in this session
        if boot_result_raw and boot_result_raw.get("failure_reason") in (
            "docker_image_build_failed",
            "image_build_failed_earlier_this_session",
        ):
            _BUILD_FAILED.add(candidate_id)
            print(f"[VERITAS] Image build failed for {candidate_id}. "
                  f"Skipping remaining runs for this tool this session.")
            print(f"[VERITAS] To retry: python run_veritas.py "
                  f"--rebuild-image {candidate_id}")

        boot_result = boot_result_raw or {
            "success": False, "wall_time_seconds": None,
            "cpu_seconds": None, "peak_ram_mb": None,
            "timeout_seconds": timeout, "timed_out": False,
            "failure_reason": "unknown", "tool_version": "unknown",
        }
        reported_ip = boot_result.pop("reported_ip", None)
    print(f"         success={boot_result['success']}  "
          f"wall={boot_result['wall_time_seconds']}s  "
          f"cpu={boot_result['cpu_seconds']}s  "
          f"ip={reported_ip}")

    # ── 3. Service probe ──────────────────────────────────────────────────────
    print(f"[VERITAS] 3/5  service probe ({reported_ip}) ...")
    if boot_result["success"] and reported_ip:
        service_result = probe_service(reported_ip=reported_ip, reported_port=80)
    else:
        service_result = {
            "reported_ip": reported_ip, "reported_port": 80,
            "tcp_connect": None, "http_status": None,
            "body_size_bytes": None, "body_sha256": None,
            "service_reachable": False, "service_authentic": False,
            "false_positive_reason": None,
        }
    print(f"         reachable={service_result['service_reachable']}  "
          f"authentic={service_result['service_authentic']}  "
          f"fp={service_result['false_positive_reason']}")

    # ── 4. Stability probe ────────────────────────────────────────────────────
    print(f"[VERITAS] 4/5  stability check (60s) ...")
    if service_result["service_reachable"] and service_result["service_authentic"]:
        stability_result = probe_stability(
            reported_ip=reported_ip, reported_port=80, wait_seconds=60)
    else:
        stability_result = {
            "probe_attempted": False,
            "service_reachable_at_60s": None,
            "service_authentic_at_60s": None,
        }
    print(f"         still_up={stability_result['service_reachable_at_60s']}")

    # ── 5. Cleanup probe ──────────────────────────────────────────────────────
    print(f"[VERITAS] 5/5  cleanup check ...")
    cleanup_result = probe_cleanup(candidate_name=candidate_id, force_clean=True)
    print(f"         clean={cleanup_result['cleanup_clean']}  "
          f"stale_taps={cleanup_result['stale_tap_devices']}  "
          f"qemu_pids={cleanup_result['orphan_qemu_pids']}")

    # ── Determine whether tool claimed unpack success ─────────────────────────
    tool_claimed_unpack = None
    if os.path.isfile(stdout_log):
        with open(stdout_log) as fh:
            tool_out = fh.read().lower()
        tool_claimed_unpack = any(phrase in tool_out for phrase in [
            "extract done", "extraction done", "get architecture done",
            "unpack success", "filesystem extracted",
        ])
    unpack_result["tool_claimed_unpack"] = tool_claimed_unpack

    # ── Assemble result ───────────────────────────────────────────────────────
    result = {
        "schema_version":    "1.0",
        "case_id":           case_id,
        "firmware_filename": case["filename"],
        "vendor":            case["vendor"],
        "tool":              candidate_id,
        "tool_name":         candidate_name,
        "tool_version":      boot_result.pop("tool_version", candidate.get("version_hint", "unknown")),
        "run_id":            run_id,
        "run_timestamp_utc": run_timestamp,
        "architecture":      architecture,
        "firmware_sha256":   case["sha256"],
        "unpack":            unpack_result,
        "boot":              boot_result,
        "service":           service_result,
        "stability":         stability_result,
        "cleanup":           cleanup_result,
        "logs": {
            "stdout_path": stdout_log,
            "stderr_path": stderr_log,
        },
        "notes": [],
    }

    write_result(result, run_id)
    return result


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="VERITAS — firmware security benchmark harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_veritas.py --firmware /path/to/firmware_folder
  python run_veritas.py --firmware /path/to/firmware_folder --candidates firmae
  python run_veritas.py --firmware /path/to/single_image.bin --candidates firmae
  python run_veritas.py --firmware /path/to/firmware_folder --dry-run
  python run_veritas.py --list-candidates
        """
    )
    parser.add_argument(
        "--firmware",
        help="Path to a folder of firmware images, or a single firmware file"
    )
    parser.add_argument(
        "--candidates", default="all",
        help="Comma-separated candidate IDs to run, or 'all' (default: all)"
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        help=f"Timeout per run in seconds (default {DEFAULT_TIMEOUT})"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would run without executing anything"
    )
    parser.add_argument(
        "--list-candidates", action="store_true",
        help="List all discovered candidates and exit"
    )
    parser.add_argument(
        "--register-tool",
        metavar="TOOL_PATH",
        help="Register a new candidate tool by pointing at its installation folder. "
             "VERITAS will fingerprint the tool automatically and create its "
             "candidates/ entry with no manual file editing required. "
             "Example: --register-tool ~/Desktop/FirmAE"
    )
    parser.add_argument(
        "--rebuild-image",
        metavar="CANDIDATE_ID",
        help="Force a rebuild of a candidate's Docker image, pulling the latest "
             "version from the tool's repository. Use this to update a tool. "
             "Example: --rebuild-image firmae"
    )
    parser.add_argument(
        "--clear-results",
        action="store_true",
        help="Delete all existing result files in results/runs/ before running. "
             "Use this when previous runs produced bad results that need to be "
             "redone from scratch. You will be asked to confirm."
    )
    args = parser.parse_args()

    # Discover all registered candidates
    all_candidates = discover_candidates()

    # --register-tool: inspect a folder and create the candidates/ entry
    if args.register_tool:
        success = register_tool(args.register_tool)
        sys.exit(0 if success else 1)

    # --clear-results: delete previous run results so they are re-run
    if args.clear_results:
        existing = list(RESULTS_DIR.glob("*.json"))
        if not existing:
            print("[VERITAS] No result files found in results/runs/ — nothing to clear.")
        else:
            print(f"[VERITAS] Found {len(existing)} result file(s) in {RESULTS_DIR}/")
            print(f"[VERITAS] These will be permanently deleted.")
            try:
                confirm = input("[VERITAS] Type 'yes' to confirm: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                confirm = ""
            if confirm == "yes":
                for f in existing:
                    f.unlink()
                print(f"[VERITAS] Cleared {len(existing)} result file(s).")
            else:
                print("[VERITAS] Cancelled — no files deleted.")
        sys.exit(0)

    # --rebuild-image: force rebuild a candidate's Docker image
    if args.rebuild_image:
        from docker_runner import DockerRunner
        candidate_id = args.rebuild_image.strip()
        if candidate_id not in all_candidates:
            print(f"[VERITAS] ERROR: Unknown candidate '{candidate_id}'", file=sys.stderr)
            print(f"[VERITAS] Available: {list(all_candidates.keys())}", file=sys.stderr)
            sys.exit(1)
        runner = DockerRunner(all_candidates[candidate_id]["conf_dir"])
        success = runner.rebuild_image()
        sys.exit(0 if success else 1)

    # --list-candidates: just print and exit
    if args.list_candidates:
        if not all_candidates:
            print("No candidates found. Add a folder to candidates/ with "
                  "run_adapter.sh and candidate.conf.")
        else:
            print(f"\nDiscovered {len(all_candidates)} candidate(s):\n")
            for cid, c in all_candidates.items():
                print(f"  {cid}")
                print(f"    Name:       {c['name']}")
                print(f"    Version:    {c['version_hint']}")
                print(f"    Signals:    {c['success_signals']}")
                print(f"    Archs:      {c['supported_architectures']}")
                print(f"    Sudo:       {c['requires_sudo']}")
                print()
        sys.exit(0)

    # --firmware is required for everything else
    if not args.firmware:
        parser.print_help()
        sys.exit(1)

    if not all_candidates:
        print("[VERITAS] ERROR: No candidates found in candidates/ folder.",
              file=sys.stderr)
        print("[VERITAS] Each candidate needs run_adapter.sh and candidate.conf.",
              file=sys.stderr)
        sys.exit(1)

    # Resolve which candidates to run
    if args.candidates.strip().lower() == "all":
        selected = all_candidates
    else:
        requested = [c.strip() for c in args.candidates.split(",")]
        missing = [r for r in requested if r not in all_candidates]
        if missing:
            print(f"[VERITAS] ERROR: Unknown candidates: {missing}", file=sys.stderr)
            print(f"[VERITAS] Available: {list(all_candidates.keys())}", file=sys.stderr)
            sys.exit(1)
        selected = {k: all_candidates[k] for k in requested}

    # Scan the firmware folder
    cases = scan_firmware_folder(args.firmware)
    if not cases:
        print(f"[VERITAS] No firmware files found in: {args.firmware}", file=sys.stderr)
        sys.exit(1)

    total_runs = len(cases) * len(selected)
    print(f"\n[VERITAS] Firmware images : {len(cases)}")
    print(f"[VERITAS] Candidates      : {list(selected.keys())}")
    print(f"[VERITAS] Total runs      : {total_runs}")
    print(f"[VERITAS] Timeout/run     : {args.timeout}s")

    # Pre-session reset for stateful tools (Finding F13)
    # Resets FirmAE database once per session, not between images
    if any("firmae" in k.lower() for k in selected.keys()):
        print("[VERITAS] FirmAE pre-session reset skipped for direct-mode test")
        # _reset_firmae_state_in_containers()
    if args.dry_run:
        print(f"[VERITAS] Mode            : DRY RUN\n")

    completed = 0
    failed    = 0

    for case in cases:
        for candidate in selected.values():
            try:
                result = run_one(
                    case=case,
                    candidate=candidate,
                    timeout=args.timeout,
                    dry_run=args.dry_run,
                )
                if result is not None:
                    completed += 1
            except KeyboardInterrupt:
                print("\n[VERITAS] Interrupted. Partial results are saved in results/runs/")
                sys.exit(0)
            except Exception as exc:
                failed += 1
                print(f"[VERITAS] ERROR — {candidate['id']} / "
                      f"{case['case_id']}: {exc}", file=sys.stderr)
                continue

    print(f"\n[VERITAS] Finished.  Completed: {completed}  Failed: {failed}")
    print(f"[VERITAS] Results in : {RESULTS_DIR}/")
    print(f"[VERITAS] Run:  python score_aggregator.py  to generate the report.")


if __name__ == "__main__":
    main()
