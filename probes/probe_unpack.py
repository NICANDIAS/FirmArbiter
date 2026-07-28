"""
probe_unpack.py
---------------
Runs an independent extraction attempt on each firmware image before
any candidate tool touches it.

This gives FIRMARBITER a ground truth about what is actually inside the
image, independent of what any tool claims to have extracted. If a
tool says it failed to unpack but FIRMARBITER's own extraction found a
root filesystem, that is a meaningful finding about the tool's
extraction capability. If neither FIRMARBITER nor the tool could extract
anything, the failure is in the image itself.

We use Binwalk for extraction because it is the de facto standard for
firmware unpacking and is already installed in your environment.

The extracted files are written to a temporary directory under
results/unpack_cache/, keyed by the firmware's SHA256. If the same
image has already been unpacked (from a previous run), we skip the
extraction and use the cached result. This avoids re-unpacking the
same image for every tool comparison.

Usage (called from run_firmarbiter.py before any tool runs):

    from probes.probe_unpack import probe_unpack
    result = probe_unpack(
        firmware_path="/path/to/image.bin",
        firmware_sha256="abc123...",
        cache_dir="results/unpack_cache",
    )
"""

import os
import subprocess
import hashlib
from pathlib import Path


# Filesystem type strings that Binwalk commonly reports in its output.
# We check for these in the extraction log to determine what types of
# filesystems were found inside the image.
KNOWN_FS_TYPES = [
    "squashfs",
    "jffs2",
    "cramfs",
    "ubifs",
    "ext2",
    "ext3",
    "ext4",
    "yaffs",
    "romfs",
]

# Minimum number of ELF files we expect to find in a proper root filesystem.
# Images with fewer than this are likely not a complete Linux filesystem
# (they might be a kernel-only image, a u-boot binary, etc.)
MIN_ELF_COUNT_FOR_ROOTFS = 3


def _find_binwalk() -> str | None:
    """
    Find the binwalk executable. Checks PATH first, then common install
    locations. Returns the full path or None if not found.
    """
    import shutil
    # Standard PATH lookup first
    found = shutil.which("binwalk")
    if found:
        return found
    # Common locations on Ubuntu where binwalk may be installed
    # but not on the venv's PATH
    for candidate in ["/usr/bin/binwalk", "/usr/local/bin/binwalk",
                       "/opt/homebrew/bin/binwalk"]:
        if os.path.isfile(candidate):
            return candidate
    return None


def _run_binwalk_extract(firmware_path: str, output_dir: str) -> tuple:
    """
    Run binwalk -e on the firmware image and return (success, stdout_output).
    Extraction output goes into output_dir.

    Tries multiple invocation styles because binwalk versions differ:
    - binwalk 2.x uses --directory and --run-as
    - binwalk 3.x changed some flags
    Both are tried so FIRMARBITER works regardless of which version is installed.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    binwalk_bin = _find_binwalk()
    if binwalk_bin is None:
        return False, "binwalk_not_found"

    # Try binwalk 2.x style first (most common on Ubuntu 22.04/24.04)
    for cmd in [
        [binwalk_bin, "--extract", "--directory", output_dir,
         "--run-as=root", "--quiet", firmware_path],
        # Binwalk 3.x dropped --run-as and changed --directory to -C
        [binwalk_bin, "--extract", "-C", output_dir,
         "--quiet", firmware_path],
        # Minimal fallback — just extract, no flags that might fail
        [binwalk_bin, "-e", firmware_path],
    ]:
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=output_dir,   # fallback extracts here
            )
            output = completed.stdout + completed.stderr
            # Consider it successful if binwalk ran without a usage error
            if completed.returncode == 0 or (
                "extracted" in output.lower() or
                "squashfs" in output.lower() or
                "filesystem" in output.lower()
            ):
                return True, output
            # If it printed a usage/option error, try the next style
            if "option" in output.lower() or "usage" in output.lower():
                continue
            # Any other non-zero exit — return what we got
            return completed.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, "binwalk_timeout"
        except FileNotFoundError:
            return False, "binwalk_not_found"

    return False, "binwalk_all_invocations_failed"


def _count_elf_binaries(search_dir: str) -> int:
    """
    Walk search_dir and count files that start with the ELF magic bytes.
    This is more reliable than checking file extensions, which many
    embedded binaries do not have.
    """
    elf_magic = b'\x7fELF'
    count = 0
    for root, _, files in os.walk(search_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, 'rb') as fh:
                    header = fh.read(4)
                    if header == elf_magic:
                        count += 1
            except (OSError, PermissionError):
                pass
    return count


def _detect_fs_types(binwalk_output: str) -> list:
    """
    Parse Binwalk's output for filesystem type strings.
    Returns a deduplicated list of types found.
    """
    found = []
    lower_output = binwalk_output.lower()
    for fs_type in KNOWN_FS_TYPES:
        if fs_type in lower_output and fs_type not in found:
            found.append(fs_type)
    return found


def _check_rootfs_present(search_dir: str) -> bool:
    """
    Check whether the extracted directory looks like a Linux root
    filesystem by looking for typical top-level directories.
    A genuine rootfs will have at least 3 of: bin, etc, lib, usr, var, tmp.
    """
    if not os.path.isdir(search_dir):
        return False

    rootfs_dirs = {"bin", "etc", "lib", "usr", "var", "tmp", "sbin"}
    found_dirs = set()

    for item in os.scandir(search_dir):
        if item.is_dir() and item.name in rootfs_dirs:
            found_dirs.add(item.name)

    return len(found_dirs) >= 3


def _detect_content_types(extraction_dir: str, binwalk_output: str) -> list:
    """
    Classify what types of content were found, beyond just rootfs present/absent.
    Returns a list of content type strings found. Possible values:

        linux_rootfs      — standard Linux directory layout found
        kernel_image      — Linux kernel binary detected
        initramfs         — initramfs/initrd embedded in kernel or standalone
        squashfs          — SquashFS filesystem found (may be rootfs inside)
        jffs2             — JFFS2 flash filesystem
        ubifs             — UBIFS flash filesystem
        cramfs             — CramFS compressed filesystem
        ext_filesystem    — ext2/ext3/ext4 filesystem image
        compressed_archive — gzip/lzma/xz/bz2 compressed data
        elf_binaries      — ELF executables found (implies some Linux content)
        uboot             — U-Boot bootloader detected
        dtb               — Device tree blob (ARM/embedded board config)
        nvram_config      — NVRAM configuration data
        certificate_store — X.509 certificates or keys found
        unknown_binary    — large binary with no recognised signatures
    """
    found = []
    lower_bw = binwalk_output.lower()

    # Filesystem types
    if "squashfs" in lower_bw:
        found.append("squashfs")
    if "jffs2" in lower_bw:
        found.append("jffs2")
    if "ubifs" in lower_bw:
        found.append("ubifs")
    if "cramfs" in lower_bw:
        found.append("cramfs")
    if any(x in lower_bw for x in ["ext2", "ext3", "ext4"]):
        found.append("ext_filesystem")

    # Kernel and boot components
    if any(x in lower_bw for x in ["linux kernel", "linux version", "vmlinuz"]):
        found.append("kernel_image")
    if any(x in lower_bw for x in ["initramfs", "initrd", "cpio archive"]):
        found.append("initramfs")
    if any(x in lower_bw for x in ["u-boot", "uboot", "das u-boot"]):
        found.append("uboot")
    if any(x in lower_bw for x in ["device tree", "dtb", "flattened device"]):
        found.append("dtb")

    # Compression (may wrap a filesystem)
    if any(x in lower_bw for x in ["gzip", "lzma", "xz compressed", "bzip2"]):
        found.append("compressed_archive")

    # Certificates and config
    if any(x in lower_bw for x in ["certificate", "x.509", "private key"]):
        found.append("certificate_store")
    if "nvram" in lower_bw:
        found.append("nvram_config")

    # Check extraction directory for ELF binaries
    elf_count = _count_elf_binaries(extraction_dir)
    if elf_count >= 3:
        found.append("elf_binaries")

    # Check for Linux rootfs layout
    rootfs_dir = _find_rootfs_dir(extraction_dir)
    if rootfs_dir:
        found.append("linux_rootfs")

    # If nothing was found but file is large, note it
    if not found:
        found.append("unknown_binary")

    return found


def _find_rootfs_dir(extraction_dir: str) -> str | None:
    """
    After Binwalk extraction, find the directory that looks most like
    a Linux root filesystem. Binwalk creates nested directories, so we
    do a shallow walk to find the first one that passes the rootfs check.
    """
    for root, dirs, _ in os.walk(extraction_dir):
        if _check_rootfs_present(root):
            return root
        # Only look two levels deep — beyond that it is unlikely to be
        # the root filesystem
        if root.count(os.sep) - extraction_dir.count(os.sep) >= 2:
            dirs.clear()
    return None


def probe_unpack(
    firmware_path: str,
    firmware_sha256: str,
    cache_dir: str = "results/unpack_cache",
) -> dict:
    """
    Independently extract and inspect a firmware image.

    Parameters
    ----------
    firmware_path : str
        Path to the firmware image file.
    firmware_sha256 : str
        SHA256 of the image (from corpus manifest). Used as cache key.
    cache_dir : str
        Directory for extraction output cache.

    Returns
    -------
    dict
        A dict matching the 'unpack' block in result_schema.json,
        minus the tool_claimed_unpack field (that is filled in by
        run_firmarbiter.py after the tool runs).
    """
    result = {
        "firmarbiter_rootfs_found": False,
        "firmarbiter_fs_types": [],
        "firmarbiter_elf_count": 0,
        "tool_claimed_unpack": None,  # filled in later
    }

    if not os.path.isfile(firmware_path):
        return result

    # Use the SHA256 as the cache key so we never unpack the same image twice
    extract_dir = os.path.join(cache_dir, firmware_sha256[:16])

    if os.path.isdir(extract_dir):
        # Cache hit — use what is already there
        binwalk_log = os.path.join(extract_dir, "binwalk_output.txt")
        binwalk_output = ""
        if os.path.isfile(binwalk_log):
            with open(binwalk_log) as fh:
                binwalk_output = fh.read()
    else:
        # Cache miss — run Binwalk
        success, binwalk_output = _run_binwalk_extract(firmware_path, extract_dir)
        # Save the output for future reference and debugging
        Path(extract_dir).mkdir(parents=True, exist_ok=True)
        with open(os.path.join(extract_dir, "binwalk_output.txt"), "w") as fh:
            fh.write(binwalk_output)

    # Analyse what came out
    result["firmarbiter_fs_types"] = _detect_fs_types(binwalk_output)

    # Detect all content types present — not just rootfs
    content_types = _detect_content_types(extract_dir, binwalk_output)
    result["firmarbiter_content_types"] = content_types
    result["firmarbiter_rootfs_found"] = "linux_rootfs" in content_types

    # ELF count — search rootfs dir first, fall back to whole extraction
    rootfs_dir = _find_rootfs_dir(extract_dir)
    if rootfs_dir:
        result["firmarbiter_elf_count"] = _count_elf_binaries(rootfs_dir)
    else:
        result["firmarbiter_elf_count"] = _count_elf_binaries(extract_dir)
        # If enough ELFs found even without standard layout, note it
        if result["firmarbiter_elf_count"] >= MIN_ELF_COUNT_FOR_ROOTFS:
            result["firmarbiter_rootfs_found"] = True
            if "elf_binaries" not in content_types:
                content_types.append("elf_binaries")

    return result
