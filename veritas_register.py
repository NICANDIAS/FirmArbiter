"""
veritas_register.py
-------------------
Handles automatic registration of candidate tools in VERITAS.

Supports two source modes for Docker image building:

  git mode   — VERITAS clones the tool from a Git repository URL when
               building the Docker image. Works for any publicly accessible
               repo. The built image always contains the latest commit from
               that repo unless you pin a specific ref.

  local mode — VERITAS copies the tool from a folder on your host machine
               into the Docker image. Use this for:
               - Private repositories you cannot make public
               - Offline environments with no internet access
               - Testing a locally modified version of the tool
               - Your own pipeline tool

When you run --register-tool, VERITAS asks which mode you want. Your
choice is saved in candidate.conf so you never have to answer again.
The Docker image is built automatically on first use in either mode.

Usage:
    python run_veritas.py --register-tool /home/ubuntu/Desktop/FirmAE
    python run_veritas.py --register-tool /home/ubuntu/firmadyne
    python run_veritas.py --register-tool /home/ubuntu/emba
    python run_veritas.py --register-tool /home/ubuntu/firmware-security

Standalone:
    python veritas_register.py /path/to/tool_folder
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path


CANDIDATES_DIR = Path("candidates")

# ── Known tool definitions ────────────────────────────────────────────────────
#
# Each entry defines how to recognise a known tool from its folder contents,
# what repo it lives in, and how to run it inside a container.
#
# tool_install_dir — where the tool lives inside the container (/opt/<name>)
# run_command_in_container — the command run_adapter.sh executes inside the
#                            container. {firmware} and {arch} are substituted.

KNOWN_TOOLS = [
    {
        "id":                     "firmae",
        "name":                   "FirmAE",
        "fingerprint_files":      ["run.sh", "sources/firmae.config"],
        "fingerprint_strings":    [("run.sh", "FirmAE"), ("run.sh", "firmae")],
        "tool_repo":              "https://github.com/pr0v3rbs/FirmAE.git",
        "tool_install_dir":       "/opt/firmae",
        "run_command_in_container": 'cd /opt/firmae && sudo ./run.sh -c "{arch}" "{firmware}"',
        "success_signals":        "Network reachable, Web service, web service on",
        "supported_archs":        "arm, mips, mipsel, mips64, x86",
        "requires_sudo":          "true",
    },
    {
        "id":                     "firmadyne",
        "name":                   "FIRMADYNE",
        "fingerprint_files":      ["scratch.sh", "firmadyne.config",
                                   "scripts/inferNetwork.py"],
        "fingerprint_strings":    [("scratch.sh", "firmadyne"),
                                   ("firmadyne.config", "FIRMADYNE")],
        "tool_repo":              "https://github.com/firmadyne/firmadyne.git",
        "tool_install_dir":       "/opt/firmadyne",
        "run_command_in_container": 'cd /opt/firmadyne && sudo bash scratch.sh "{firmware}"',
        "success_signals":        "Network reachable, web service, infer network done",
        "supported_archs":        "arm, mips, mipsel, x86",
        "requires_sudo":          "true",
    },
    {
        "id":                     "emba",
        "name":                   "EMBA",
        "fingerprint_files":      ["emba.sh", "modules", "helpers/helpers.sh"],
        "fingerprint_strings":    [("emba.sh", "EMBA"), ("emba.sh", "emba")],
        "tool_repo":              "https://github.com/e-m-b-a/emba.git",
        "tool_install_dir":       "/opt/emba",
        "run_command_in_container": (
            'cd /opt/emba && LOG_DIR="/opt/emba/emba_logs/run_$(date +%s)" && '
            'mkdir -p "$LOG_DIR" && '
            'sudo ./emba.sh -f "{firmware}" -l "$LOG_DIR" -A "{arch}" -e'
        ),
        "success_signals":        "finished, analysis done, emba finished, Test ended",
        "supported_archs":        "arm, mips, mipsel, x86, mips64",
        "requires_sudo":          "true",
    },
]


# ── Fingerprinting ─────────────────────────────────────────────────────────────

def _file_contains(file_path: Path, text: str) -> bool:
    try:
        return text.lower() in file_path.read_text(errors="replace").lower()
    except OSError:
        return False


def fingerprint_tool(tool_dir: Path) -> dict | None:
    """
    Inspect tool_dir and return the matching known tool dict, or None.
    Requires both a file-presence match and a content-string match.
    """
    for tool in KNOWN_TOOLS:
        file_match = any(
            (tool_dir / f).exists() for f in tool["fingerprint_files"]
        )
        if not file_match:
            continue

        string_match = any(
            _file_contains(tool_dir / fname, string)
            for fname, string in tool["fingerprint_strings"]
            if (tool_dir / fname).exists()
        )
        if string_match:
            return tool

    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ask(prompt: str, default: str = "") -> str:
    """Prompt the user for input. Returns default on empty answer."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
        return answer if answer else default
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def _ask_choice(prompt: str, choices: list, default: str) -> str:
    """Ask the user to choose from a list of options."""
    choices_str = "/".join(choices)
    try:
        answer = input(f"{prompt} ({choices_str}) [{default}]: ").strip().lower()
        return answer if answer in choices else default
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def _git_short_commit(tool_dir: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(tool_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "local"


# ── Dockerfile generation ─────────────────────────────────────────────────────

def _generate_dockerfile(tool_id: str, tool_name: str,
                          tool_source: str, tool_repo: str) -> str:
    """
    Generate a Dockerfile for an unrecognised tool.
    For known tools the Dockerfile already exists in candidates/ and is used as-is.
    This generator is only called for tools registered interactively.
    """
    git_block = f"""
RUN if [ "$TOOL_SOURCE" = "git" ]; then \\
        git clone --depth 1 "$TOOL_REPO" tool && \\
        cd tool && git rev-parse HEAD > .veritas_commit; \\
    fi"""

    local_block = f"""
COPY --from=tool_src . /opt/tool_local_staging/

RUN if [ "$TOOL_SOURCE" = "local" ]; then \\
        cp -r /opt/tool_local_staging /opt/tool && \\
        if [ -d /opt/tool/.git ]; then \\
            cd /opt/tool && git rev-parse HEAD > .veritas_commit 2>/dev/null || \\
            echo "local-copy-no-git" > /opt/tool/.veritas_commit; \\
        else \\
            echo "local-copy-no-git" > /opt/tool/.veritas_commit; \\
        fi; \\
    fi"""

    return f"""# candidates/{tool_id}/Dockerfile
# Auto-generated by VERITAS for {tool_name}
# Supports TOOL_SOURCE=git and TOOL_SOURCE=local

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

ARG TOOL_SOURCE={tool_source}
ARG TOOL_REPO={tool_repo}

RUN apt-get update -qq && apt-get install -y --no-install-recommends \\
    git wget curl sudo python3 python3-pip \\
    net-tools iproute2 unzip \\
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
{git_block}
{local_block}

# Add tool-specific packages to actual_deps.txt as you discover them.
# Add them here to the RUN apt-get line above.

COPY run_adapter.sh /opt/run_adapter.sh
RUN chmod +x /opt/run_adapter.sh

ENTRYPOINT ["bash", "/opt/run_adapter.sh"]
"""


# ── Adapter generation ────────────────────────────────────────────────────────

def _generate_adapter(tool_name: str, run_command: str) -> str:
    """Generate run_adapter.sh content for a tool."""
    return f"""#!/usr/bin/env bash
# run_adapter.sh — auto-generated by VERITAS for {tool_name}
# Runs inside the Docker container. Called by VERITAS as:
#   bash /opt/run_adapter.sh <firmware_path> <architecture>

set -euo pipefail

FIRMWARE="${{1:-}}"
ARCH="${{2:-}}"

if [ -z "$FIRMWARE" ] || [ -z "$ARCH" ]; then
    echo "[VERITAS][{tool_name}] ERROR: usage: run_adapter.sh <firmware_path> <architecture>" >&2
    exit 3
fi

if [ ! -f "$FIRMWARE" ]; then
    echo "[VERITAS][{tool_name}] ERROR: firmware not found: $FIRMWARE" >&2
    exit 3
fi

echo "[VERITAS][{tool_name}] Starting on: $(basename "$FIRMWARE") ($ARCH)"

{run_command.replace('{firmware}', '"$FIRMWARE"').replace('{arch}', '"$ARCH"')}

EXIT_CODE=$?
echo "[VERITAS][{tool_name}] Exited with code: $EXIT_CODE"
exit $EXIT_CODE
"""


def _generate_candidate_conf(tool_id: str, tool_name: str,
                              tool_source: str, tool_repo: str,
                              tool_local_path: str,
                              success_signals: str, supported_archs: str,
                              requires_sudo: str) -> str:
    """Generate candidate.conf content."""
    local_line = (f"tool_local_path = {tool_local_path}\n"
                  if tool_source == "local" and tool_local_path else "")
    repo_line  = (f"tool_repo = {tool_repo}\n"
                  if tool_repo else "")
    return f"""# candidate.conf — auto-generated by VERITAS for {tool_name}
# Edit any value below if needed. Re-register to regenerate.

name = {tool_name}
id = {tool_id}
tool_source = {tool_source}
{repo_line}{local_line}
success_signals = {success_signals}
supported_architectures = {supported_archs}
requires_sudo = {requires_sudo}
"""


# ── Interactive registration ──────────────────────────────────────────────────

def _register_interactively(tool_dir: Path) -> dict | None:
    """
    Ask the user enough questions to register an unrecognised tool.
    Returns the registration dict or None if cancelled.
    """
    print(f"\n[VERITAS] Tool not recognised from fingerprints.")
    print(f"[VERITAS] Answer a few questions to register it. (Ctrl+C to cancel)\n")

    try:
        default_id = tool_dir.name.lower().replace("-", "_").replace(" ", "_")
        tool_id   = _ask("Short ID (lowercase, no spaces)", default=default_id)
        if not tool_id:
            return None

        tool_name = _ask("Human-readable name", default=tool_dir.name)

        print(f"\nWhat does this tool print when emulation succeeds?")
        print(f"(Check its terminal output from your manual test runs.)")
        print(f"Enter comma-separated phrases, e.g.: Network reachable, web service\n")
        signals = _ask("Success signals", default="emulation complete, analysis done")

        archs = _ask("Supported architectures (comma-separated)",
                     default="arm, mips, mipsel, x86")

        sudo_yn = _ask("Does this tool need sudo to run? (y/n)", default="y")
        requires_sudo = "true" if sudo_yn.lower().startswith("y") else "false"

        print(f"\nWhat command runs this tool inside the container?")
        print(f"Use {{firmware}} for the firmware file path and {{arch}} for the arch.")
        print(f"The tool will be at /opt/tool/ inside the container.\n")
        run_cmd = _ask(
            "Run command",
            default=f'cd /opt/tool && bash run.sh "{{firmware}}"'
        )

    except KeyboardInterrupt:
        print("\n[VERITAS] Registration cancelled.")
        return None

    return {
        "id":           tool_id,
        "name":         tool_name,
        "tool_repo":    "",
        "success_signals": signals,
        "supported_archs": archs,
        "requires_sudo":   requires_sudo,
        "run_command_in_container": run_cmd,
        "tool_install_dir": "/opt/tool",
    }


# ── Source mode selection ─────────────────────────────────────────────────────

def _ask_source_mode(tool_dir: Path, known_repo: str) -> tuple[str, str, str]:
    """
    Ask the user whether to use the local folder or clone from a repo.

    Returns (tool_source, tool_repo, tool_local_path).
    """
    print(f"\n[VERITAS] How should VERITAS install this tool inside Docker?\n")
    print(f"  local  — copy from this folder: {tool_dir}")
    if known_repo:
        print(f"  git    — clone from repo: {known_repo}")
    else:
        print(f"  git    — clone from a Git repository URL you provide")
    print()

    # Default to local if the folder looks like a local install
    # Default to git if we know the repo URL
    default = "git" if known_repo else "local"
    choice  = _ask_choice("Source mode", ["local", "git"], default=default)

    if choice == "local":
        return "local", known_repo, str(tool_dir)

    else:  # git
        if known_repo:
            repo = _ask("Repository URL", default=known_repo)
        else:
            repo = _ask("Repository URL (e.g. https://github.com/author/tool.git)")
            if not repo:
                print("[VERITAS] No repo URL provided. Falling back to local mode.")
                return "local", "", str(tool_dir)
        return "git", repo, ""


# ── Main registration function ────────────────────────────────────────────────

def register_tool(tool_path: str) -> bool:
    """
    Register a tool at tool_path as a VERITAS candidate.

    Steps:
      1. Fingerprint the folder to identify the tool
      2. Ask whether to use local copy or Git repo
      3. Write candidates/<id>/ with all required files
    """
    tool_dir = Path(tool_path).resolve()

    if not tool_dir.exists():
        print(f"[VERITAS] ERROR: Path not found: {tool_dir}", file=sys.stderr)
        return False

    if not tool_dir.is_dir():
        print(f"[VERITAS] ERROR: Expected a folder: {tool_dir}", file=sys.stderr)
        return False

    print(f"\n[VERITAS] Inspecting: {tool_dir}")

    # Step 1 — fingerprint
    known = fingerprint_tool(tool_dir)

    if known:
        print(f"[VERITAS] Recognised: {known['name']}")
        registration = dict(known)
    else:
        registration = _register_interactively(tool_dir)
        if registration is None:
            return False

    # Step 2 — ask about source mode
    try:
        tool_source, tool_repo, tool_local_path = _ask_source_mode(
            tool_dir,
            known_repo=registration.get("tool_repo", ""),
        )
    except KeyboardInterrupt:
        print("\n[VERITAS] Registration cancelled.")
        return False

    # Step 3 — write candidates/ folder
    candidate_dir = CANDIDATES_DIR / registration["id"]

    if candidate_dir.exists():
        print(f"\n[VERITAS] '{registration['id']}' is already registered.")
        try:
            overwrite = _ask("Overwrite? (y/n)", default="n")
        except KeyboardInterrupt:
            print()
            return False
        if not overwrite.lower().startswith("y"):
            print("[VERITAS] Registration skipped.")
            return False
        shutil.rmtree(candidate_dir)

    candidate_dir.mkdir(parents=True)

    # Write .local_path so DockerRunner can find the folder later
    if tool_source == "local":
        (candidate_dir / ".local_path").write_text(str(tool_dir))

    # Write candidate.conf
    conf = _generate_candidate_conf(
        tool_id=registration["id"],
        tool_name=registration["name"],
        tool_source=tool_source,
        tool_repo=tool_repo,
        tool_local_path=tool_local_path,
        success_signals=registration["success_signals"],
        supported_archs=registration["supported_archs"],
        requires_sudo=registration["requires_sudo"],
    )
    (candidate_dir / "candidate.conf").write_text(conf)

    # Write run_adapter.sh
    adapter = _generate_adapter(
        tool_name=registration["name"],
        run_command=registration["run_command_in_container"],
    )
    adapter_path = candidate_dir / "run_adapter.sh"
    adapter_path.write_text(adapter)
    adapter_path.chmod(0o755)

    # Copy existing Dockerfile if the tool is known (the pre-built one),
    # otherwise generate a generic one
    src_dockerfile = Path("candidates") / registration["id"] / "Dockerfile"
    # For known tools the Dockerfile was already there before registration.
    # The shutil.rmtree above removed it, so we need to write a fresh one.
    df_content = _generate_dockerfile(
        tool_id=registration["id"],
        tool_name=registration["name"],
        tool_source=tool_source,
        tool_repo=tool_repo,
    )
    (candidate_dir / "Dockerfile").write_text(df_content)

    # Write empty dep files
    (candidate_dir / "declared_deps.txt").write_text(
        f"# Declared dependencies for {registration['name']}\n"
        f"# Copy from the tool's own documentation or README.\n"
    )
    (candidate_dir / "actual_deps.txt").write_text(
        f"# Actual dependencies for {registration['name']}\n"
        f"# Record every package you add to the Dockerfile to make it work.\n"
        f"# The gap between this file and declared_deps.txt is a benchmark finding.\n"
    )

    # Print summary
    source_desc = (f"local folder: {tool_dir}" if tool_source == "local"
                   else f"git repo: {tool_repo}")
    print(f"\n[VERITAS] Registered '{registration['name']}' successfully.")
    print(f"[VERITAS] Source: {source_desc}")
    print(f"[VERITAS] Folder: {candidate_dir}/")
    print(f"[VERITAS] Files:")
    for f in sorted(candidate_dir.iterdir()):
        if not f.name.startswith("."):
            print(f"[VERITAS]   {f.name}")
    print(f"\n[VERITAS] Next: python run_veritas.py --list-candidates")
    print(f"[VERITAS] The Docker image will be built automatically on first run.")

    return True


# ── Standalone usage ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python veritas_register.py /path/to/tool_folder")
        sys.exit(1)
    success = register_tool(sys.argv[1])
    sys.exit(0 if success else 1)
