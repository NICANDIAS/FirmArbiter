#!/usr/bin/env python3
"""
tools/assess_candidate.py — pre-onboarding compatibility assessment.

Automates the checks FIRMARBITER engineers had to discover manually and
expensively while onboarding EMBA (bare-host/DinD assumptions, multi-service
architecture, architecture-hardcoded install scripts, mid-install reboots).
A user points this at a candidate tool's repo BEFORE writing any adapter
code, and gets a structured PASS/WARN/FAIL report — no research required.

Usage:
    python tools/assess_candidate.py <path_or_git_url> [--output report.md]

If given a git URL, clones to a temp dir first. If given a local path,
assesses it in place.
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CheckResult:
    name: str
    status: str  # "PASS", "WARN", "FAIL", "INFO"
    detail: str
    evidence: list = field(default_factory=list)


def clone_if_url(target):
    if target.startswith("http://") or target.startswith("https://") or target.endswith(".git"):
        tmp_dir = tempfile.mkdtemp(prefix="firmarbiter_assess_")
        print(f"Cloning {target} to {tmp_dir} ...")
        result = subprocess.run(
            ["git", "clone", "--depth", "1", target, tmp_dir],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"FATAL: git clone failed: {result.stderr}", file=sys.stderr)
            sys.exit(1)
        return Path(tmp_dir), target
    else:
        p = Path(target).resolve()
        if not p.exists():
            print(f"FATAL: path does not exist: {target}", file=sys.stderr)
            sys.exit(1)
        return p, None


def find_dockerfiles(repo_path):
    return list(repo_path.rglob("Dockerfile")) + list(repo_path.rglob("*.dockerfile"))


def find_compose_files(repo_path):
    patterns = ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]
    found = []
    for pattern in patterns:
        found.extend(repo_path.rglob(pattern))
    return found


def check_single_container_buildability(repo_path):
    dockerfiles = find_dockerfiles(repo_path)
    if len(dockerfiles) == 0:
        return CheckResult(
            "Single-container buildability", "FAIL",
            "No Dockerfile found anywhere in the repo. Candidate may not be "
            "containerizable at all, or uses a non-standard build system.",
        )
    if len(dockerfiles) == 1:
        return CheckResult(
            "Single-container buildability", "PASS",
            f"Exactly one Dockerfile found: {dockerfiles[0].relative_to(repo_path)}",
        )
    return CheckResult(
        "Single-container buildability", "WARN",
        f"{len(dockerfiles)} Dockerfiles found — candidate may build multiple "
        f"images/services rather than a single self-contained container.",
        evidence=[str(f.relative_to(repo_path)) for f in dockerfiles],
    )


def check_multiservice_architecture(repo_path):
    compose_files = find_compose_files(repo_path)
    if not compose_files:
        return CheckResult(
            "Multi-service architecture", "PASS",
            "No docker-compose file found — candidate likely runs as a single container.",
        )
    max_services = 0
    evidence = []
    for cf in compose_files:
        try:
            import yaml
            data = yaml.safe_load(cf.read_text())
            services = data.get("services", {}) if isinstance(data, dict) else {}
            n = len(services)
            max_services = max(max_services, n)
            evidence.append(f"{cf.relative_to(repo_path)}: {n} service(s) — {list(services.keys())}")
        except Exception as e:
            evidence.append(f"{cf.relative_to(repo_path)}: could not parse ({e})")
    if max_services <= 1:
        return CheckResult(
            "Multi-service architecture", "PASS",
            "docker-compose file(s) present but define only a single service.",
            evidence=evidence,
        )
    return CheckResult(
        "Multi-service architecture", "WARN",
        f"docker-compose defines {max_services} services. This suggests a "
        f"persistent multi-component application (e.g. separate DB/frontend/"
        f"backend), not a single-shot batch tool — a materially different "
        f"shape than the Adapter Contract's run-once-per-firmware model.",
        evidence=evidence,
    )


def check_bare_host_dind_assumptions(repo_path):
    patterns = [
        r"/var/run/docker\.sock",
        r"\bdocker\s+(run|images|ps|create)\b",
    ]
    hits = []
    script_extensions = {".sh", ""}  # "" matches extensionless scripts
    candidates = [f for f in repo_path.rglob("*") if f.is_file() and
                  (f.suffix in script_extensions or f.name in ("Dockerfile",))]
    for f in candidates:
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        for pattern in patterns:
            if re.search(pattern, text):
                hits.append(f"{f.relative_to(repo_path)}: matches /{pattern}/")
                break
    if not hits:
        return CheckResult(
            "Bare-host / Docker-in-Docker assumptions", "PASS",
            "No references to docker.sock or nested docker commands found in scripts.",
        )
    return CheckResult(
        "Bare-host / Docker-in-Docker assumptions", "WARN",
        f"Found {len(hits)} script(s) referencing the Docker socket or "
        f"docker CLI commands directly. This is the exact pattern that broke "
        f"EMBA onboarding — the tool may expect to orchestrate sibling "
        f"containers itself rather than running self-contained.",
        evidence=hits[:10],
    )


def check_architecture_hardcoding(repo_path):
    pattern = re.compile(r"[_\-](amd64|x86_64)[_.]")
    arch_var_pattern = re.compile(r"dpkg --print-architecture|\$\{?ARCH\}?|uname -m")
    hits = []
    candidates = list(repo_path.rglob("*.sh")) + find_dockerfiles(repo_path)
    for f in candidates:
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line) and not arch_var_pattern.search(line):
                hits.append(f"{f.relative_to(repo_path)}:{line_no}: {line.strip()[:100]}")
    if not hits:
        return CheckResult(
            "Architecture-hardcoding", "PASS",
            "No hardcoded amd64/x86_64 references found without architecture detection.",
        )
    return CheckResult(
        "Architecture-hardcoding", "WARN",
        f"Found {len(hits)} line(s) with hardcoded amd64/x86_64 references "
        f"and no architecture-detection logic nearby. This is exactly the "
        f"class of bug found in EMBA's installer (sasquatch, libfuse2t64, "
        f"uml-utilities) — expect ARM64 build failures unless verified otherwise.",
        evidence=hits[:15],
    )


def check_privileged_or_device_requirements(repo_path):
    hits = []
    compose_files = find_compose_files(repo_path)
    for cf in compose_files:
        text = cf.read_text(errors="ignore")
        if "privileged" in text or "/dev" in text or "devices:" in text:
            hits.append(f"{cf.relative_to(repo_path)}: references privileged/device access")
    for f in find_dockerfiles(repo_path):
        text = f.read_text(errors="ignore")
        if "--privileged" in text:
            hits.append(f"{f.relative_to(repo_path)}: references --privileged")
    # Also check documentation — many tools (e.g. fact_extractor) only
    # document --privileged/-v /dev:/dev in README usage examples, not in
    # any Dockerfile or compose file. Missing this produced a false
    # negative during real-world testing.
    doc_files = list(set(repo_path.rglob("README*")) | set(repo_path.rglob("*.md")))
    for f in doc_files:
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        if "--privileged" in text or re.search(r"-v\s+/dev:/dev", text):
            hits.append(f"{f.relative_to(repo_path)}: documents --privileged / /dev mount in usage instructions")
    if not hits:
        return CheckResult(
            "Privileged / device requirements", "INFO",
            "No privileged mode or device mount requirements detected.",
        )
    return CheckResult(
        "Privileged / device requirements", "INFO",
        f"Candidate requires privileged mode and/or device access — common "
        f"and legitimate for firmware tools (FirmAE, FIRMADYNE, EMBA, "
        f"fact_extractor all need this). Not a red flag by itself, but "
        f"confirm your adapter's runtime section documents it.",
        evidence=hits,
    )


def check_install_time_reboot(repo_path):
    hits = []
    doc_files = list(repo_path.rglob("*.md")) + list(repo_path.rglob("INSTALL*"))
    for f in doc_files:
        try:
            text = f.read_text(errors="ignore").lower()
        except Exception:
            continue
        if re.search(r"\breboot\b", text):
            hits.append(f"{f.relative_to(repo_path)}: mentions 'reboot'")
    if not hits:
        return CheckResult(
            "Install-time reboot requirement", "PASS",
            "No documentation references a required reboot during installation.",
        )
    return CheckResult(
        "Install-time reboot requirement", "FAIL",
        f"Found {len(hits)} doc file(s) mentioning a required reboot during "
        f"install. This is incompatible with a Dockerized, single-build "
        f"adapter — the tool likely expects bare-metal host installation.",
        evidence=hits,
    )


def check_maintenance_signal(git_url):
    if not git_url or "github.com" not in git_url:
        return CheckResult(
            "Maintenance signal", "INFO",
            "Not a GitHub URL — skipping automated maintenance check. "
            "Verify manually (recent commits, open issues addressed).",
        )
    match = re.search(r"github\.com/([^/]+)/([^/.]+)", git_url)
    if not match:
        return CheckResult("Maintenance signal", "INFO", "Could not parse GitHub owner/repo from URL.")
    owner, repo = match.group(1), match.group(2)
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        with urllib.request.urlopen(api_url, timeout=10) as response:
            data = json.loads(response.read())
        pushed_at = data.get("pushed_at", "unknown")
        open_issues = data.get("open_issues_count", "unknown")
        archived = data.get("archived", False)
        if archived:
            return CheckResult(
                "Maintenance signal", "FAIL",
                f"Repository is ARCHIVED on GitHub. Last push: {pushed_at}.",
            )
        return CheckResult(
            "Maintenance signal", "PASS",
            f"Last pushed: {pushed_at}. Open issues: {open_issues}. Not archived.",
        )
    except Exception as e:
        return CheckResult(
            "Maintenance signal", "INFO",
            f"Could not reach GitHub API to check maintenance status ({e}). Verify manually.",
        )


def run_all_checks(repo_path, git_url):
    return [
        check_single_container_buildability(repo_path),
        check_multiservice_architecture(repo_path),
        check_bare_host_dind_assumptions(repo_path),
        check_architecture_hardcoding(repo_path),
        check_privileged_or_device_requirements(repo_path),
        check_install_time_reboot(repo_path),
        check_maintenance_signal(git_url),
    ]


def render_report(results, candidate_name):
    lines = [f"# FIRMARBITER Compatibility Assessment: {candidate_name}", ""]
    status_counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "INFO": 0}
    for r in results:
        status_counts[r.status] += 1

    lines.append(f"**Summary:** {status_counts['FAIL']} FAIL, {status_counts['WARN']} WARN, "
                  f"{status_counts['PASS']} PASS, {status_counts['INFO']} INFO")
    lines.append("")

    if status_counts["FAIL"] > 0:
        lines.append("⚠️ **One or more FAIL results — this candidate likely needs significant "
                      "architectural rework before it fits the Adapter Contract cleanly.**")
    elif status_counts["WARN"] > 0:
        lines.append("⚠️ **WARN results present — review carefully before proceeding; "
                      "these historically predicted real onboarding friction (see EMBA).**")
    else:
        lines.append("✅ **No FAIL/WARN results — this candidate looks like a straightforward fit.**")
    lines.append("")

    for r in results:
        icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "INFO": "ℹ️"}[r.status]
        lines.append(f"## {icon} {r.name} — {r.status}")
        lines.append(r.detail)
        if r.evidence:
            lines.append("")
            lines.append("Evidence:")
            for e in r.evidence:
                lines.append(f"- `{e}`")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Assess a candidate tool's compatibility with the FIRMARBITER Adapter Contract")
    parser.add_argument("target", help="Local path or git URL of the candidate tool's repo")
    parser.add_argument("--output", default=None, help="Path to save the markdown report (default: print to stdout only)")
    args = parser.parse_args()

    repo_path, git_url = clone_if_url(args.target)
    candidate_name = repo_path.name if not git_url else git_url.rstrip("/").split("/")[-1].replace(".git", "")

    results = run_all_checks(repo_path, git_url)
    report = render_report(results, candidate_name)

    print(report)

    if args.output:
        Path(args.output).write_text(report)
        print(f"\nReport saved to {args.output}")

    fail_count = sum(1 for r in results if r.status == "FAIL")
    sys.exit(1 if fail_count > 0 else 0)


if __name__ == "__main__":
    main()
