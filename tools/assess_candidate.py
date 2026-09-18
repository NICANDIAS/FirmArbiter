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


def find_vendored_roots(repo_path):
    """
    Finds subdirectories that look like an entire OTHER project vendored
    into this repo, rather than the candidate's own code.

    Why this matters: assessing Greenhouse for real surfaced this
    directly. Most of that run's WARN evidence -- a Docker-socket
    reference, a Python 3.6 classifier -- traced not to Greenhouse's own
    code but to routersploit_gh/routersploit_ghpatched/, a complete
    second tool (RouterSploit) bundled inside the repo. Evidence from
    vendored code is real, but it's a different KIND of finding than
    evidence from the candidate's own code -- it may never even run as
    part of what your adapter invokes. Without distinguishing the two,
    every check's evidence conflates "the candidate's own code has this
    problem" with "some unrelated tool bundled three directories deep
    has this problem", which is misleading in exactly the way that
    happened here.

    Three signals, in order of reliability:
      1. .gitmodules at the repo root -- an explicit, structured
         declaration. Most reliable by far.
      2. A subdirectory containing its own .git (file or directory) --
         the hallmark of a nested clone that was copied in whole,
         history included.
      3. A subdirectory (not the repo root itself) containing its own
         setup.py or pyproject.toml -- a strong signal of "this is a
         separate Python package", since a candidate's own code doesn't
         typically define a second, independent package one level down
         from its own.

    This is a heuristic, not certainty -- a monorepo with genuinely
    first-party sub-packages would also match signal 3. When in doubt
    it labels rather than silently hides; see how evidence lines use
    this in the checks below.
    """
    vendored = set()

    gitmodules = repo_path / ".gitmodules"
    if gitmodules.exists():
        try:
            text = gitmodules.read_text(errors="ignore")
        except Exception:
            text = ""
        for match in re.finditer(r"path\s*=\s*(\S+)", text):
            candidate_path = (repo_path / match.group(1)).resolve()
            if candidate_path.is_dir():
                vendored.add(candidate_path)

    for git_marker in list(repo_path.rglob(".git")):
        if git_marker.parent == repo_path:
            continue  # the repo's own .git, not a vendored one
        vendored.add(git_marker.parent)

    for marker_name in ("setup.py", "pyproject.toml"):
        for marker in repo_path.rglob(marker_name):
            if marker.parent == repo_path:
                continue  # the candidate's own top-level package, not vendored
            # Only the OUTERMOST such directory counts as a vendored
            # root -- routersploit_gh/routersploit_ghpatched/setup.py
            # should mark routersploit_gh/, not add a second, redundant
            # nested root underneath it.
            already_covered = any(
                marker.parent == v or v in marker.parent.parents
                for v in vendored
            )
            if not already_covered:
                vendored.add(marker.parent)

    return vendored


def label_evidence(path, repo_path, vendored_roots):
    """
    Prefixes an evidence line's path with [vendored: <root>] or
    [own code] so a WARN/FAIL's evidence is honest about which kind of
    code it's pointing at, rather than presenting a hit three directories
    inside a bundled third-party tool exactly the same way as a hit in
    the candidate's own top-level source.
    """
    resolved = path.resolve()
    for root in vendored_roots:
        if resolved == root or root in resolved.parents:
            return f"[vendored: {root.relative_to(repo_path)}] {path.relative_to(repo_path)}"
    return f"[own code] {path.relative_to(repo_path)}"


def check_vendored_code(repo_path, vendored_roots):
    if not vendored_roots:
        return CheckResult(
            "Vendored / third-party code", "INFO",
            "No vendored third-party projects detected (no .gitmodules, "
            "no nested .git, no nested setup.py/pyproject.toml one or "
            "more directories in). Other checks' evidence below can be "
            "read as the candidate's own code without qualification.",
        )
    repo_relative = sorted(
        str(root.relative_to(repo_path)) for root in vendored_roots
    )
    return CheckResult(
        "Vendored / third-party code", "INFO",
        f"{len(vendored_roots)} vendored third-party director"
        f"{'y' if len(vendored_roots) == 1 else 'ies'} detected. Other "
        f"checks below label their evidence '[own code]' or "
        f"'[vendored: ...]' accordingly -- a finding inside vendored "
        f"code is real, but it's evidence about a bundled dependency, "
        f"not necessarily about what the candidate's own code does or "
        f"what your adapter will actually invoke. Worth confirming "
        f"whether your adapter's pipeline even touches these paths "
        f"before treating a vendored-code WARN as equally urgent to an "
        f"own-code one.",
        evidence=repo_relative,
    )


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


def check_bare_host_dind_assumptions(repo_path, vendored_roots):
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
                hits.append(f"{label_evidence(f, repo_path, vendored_roots)}: matches /{pattern}/")
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


def check_architecture_hardcoding(repo_path, vendored_roots):
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
                hits.append(f"{label_evidence(f, repo_path, vendored_roots)}:{line_no}: {line.strip()[:100]}")
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


def check_privileged_or_device_requirements(repo_path, vendored_roots):
    hits = []
    compose_files = find_compose_files(repo_path)
    for cf in compose_files:
        text = cf.read_text(errors="ignore")
        if "privileged" in text or "/dev" in text or "devices:" in text:
            hits.append(f"{label_evidence(cf, repo_path, vendored_roots)}: references privileged/device access")
    for f in find_dockerfiles(repo_path):
        text = f.read_text(errors="ignore")
        if "--privileged" in text:
            hits.append(f"{label_evidence(f, repo_path, vendored_roots)}: references --privileged")
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
            hits.append(f"{label_evidence(f, repo_path, vendored_roots)}: documents --privileged / /dev mount in usage instructions")
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


def check_python_version_compatibility(repo_path, vendored_roots):
    """
    Looks for the candidate's OWN declared Python version requirements and
    flags a likely mismatch against what a fresh adapter's base image
    actually provides by default.

    This is exactly the category of problem that caused the real,
    hours-long debugging session onboarding Greenhouse: old pinned
    dependencies breaking under a newer Python's restructured exception
    internals (lxml, urllib3, six all hit this). A mismatch caught here,
    before any Docker build, is minutes of reading instead of hours of
    reproducing a stack trace to figure out it was the Python version all
    along.

    Heuristic text scan of the common places a Python version constraint
    gets declared -- not a resolver, and not proof either way. It tells
    you where to look, not that the candidate will definitely work or
    fail. Doctor diagnoses; it doesn't decide for you.
    """
    # adapters/_template/Dockerfile pins FROM ubuntu:22.04 and installs
    # python3 via plain `apt-get install python3` with no version pin --
    # Ubuntu 22.04 (Jammy)'s python3 package is 3.10.x. If your adapter's
    # own Dockerfile changes this (a different base image, a PPA, a
    # source build), this comparison no longer applies -- it's checking
    # the TEMPLATE default, not your adapter's actual choice.
    template_python = "3.10 (Ubuntu 22.04 jammy's default python3 package)"

    sources = {
        "setup.py": re.compile(r"python_requires\s*=\s*['\"]([^'\"]+)['\"]"),
        "setup.cfg": re.compile(r"python_requires\s*=\s*([^\n]+)"),
        "pyproject.toml": re.compile(r"requires-python\s*=\s*['\"]([^'\"]+)['\"]"),
    }
    classifier_pattern = re.compile(
        r"Programming Language :: Python :: (\d+\.\d+)"
    )

    findings = []
    for filename, pattern in sources.items():
        for f in repo_path.rglob(filename):
            try:
                text = f.read_text(errors="ignore")
            except Exception:
                continue
            match = pattern.search(text)
            if match:
                findings.append(
                    f"{label_evidence(f, repo_path, vendored_roots)}: "
                    f"requires-python '{match.group(1).strip()}'"
                )
            classifiers = classifier_pattern.findall(text)
            if classifiers:
                findings.append(
                    f"{label_evidence(f, repo_path, vendored_roots)}: "
                    f"classifiers list Python {', '.join(sorted(set(classifiers)))}"
                )

    for f in repo_path.rglob(".python-version"):
        try:
            version = f.read_text(errors="ignore").strip()
        except Exception:
            continue
        if version:
            findings.append(f"{label_evidence(f, repo_path, vendored_roots)}: pins {version}")

    if not findings:
        return CheckResult(
            "Python version compatibility", "INFO",
            "No explicit Python version constraint found (setup.py/"
            "setup.cfg/pyproject.toml python_requires or classifiers, "
            ".python-version). Either the candidate doesn't pin one, or "
            "it's declared somewhere this scan doesn't look. Worth a "
            "manual check before assuming compatibility.",
        )

    return CheckResult(
        "Python version compatibility", "WARN",
        f"Candidate declares Python version constraints. A fresh adapter "
        f"gets Python {template_python} unless your Dockerfile changes "
        f"that. Compare the declared constraint(s) below against that "
        f"before assuming pip install will just work -- an old upper "
        f"bound (e.g. '<3.10' or a classifier list that stops at 3.8) is "
        f"a strong signal you'll hit the exact class of dependency "
        f"breakage that made onboarding Greenhouse take as long as it "
        f"did. Check each finding's [own code] / [vendored: ...] label "
        f"below first, though -- a constraint declared only inside "
        f"vendored code says nothing about the candidate's own "
        f"compatibility.",
        evidence=findings,
    )


def check_dependency_file_conflicts(repo_path, vendored_roots):
    """
    Finds every requirements*.txt-shaped file in the repo and flags:
      (a) more than one existing at all (which one is authoritative?),
      (b) the same package pinned to CONFLICTING versions across files.

    This is a simple exact-pin comparison (==X vs ==Y for the same
    package name), not a real dependency resolver -- it will miss
    resolvable range conflicts and can't tell you which pin is "right".
    What it catches is the specific, real pattern that cost real time
    during onboarding: two requirements files quietly disagreeing with
    each other, discovered only after a confusing pip install failure.

    Distinguishes an OWN-vs-VENDORED conflict (the candidate's own
    requirements.txt disagreeing with a bundled third-party tool's) from
    a conflict entirely within vendored code. The first is exactly the
    real pattern found assessing Greenhouse (its own requirements.txt
    wanted requests==2.24.0; a bundled RouterSploit copy wanted
    2.21.0) -- genuinely actionable, since pip installing both means one
    silently wins depending on order, and it affects a package the
    candidate's own code presumably imports. A conflict entirely inside
    vendored code is lower-stakes: both files may not even get installed
    as part of what your adapter actually runs.
    """
    pin_pattern = re.compile(
        r"^\s*([A-Za-z0-9_.\-]+)\s*==\s*([A-Za-z0-9_.\-]+)"
    )

    req_files = sorted(
        set(repo_path.rglob("*requirements*.txt"))
        - set(repo_path.rglob("*/node_modules/*"))
    )

    if not req_files:
        return CheckResult(
            "Dependency file conflicts", "INFO",
            "No *requirements*.txt files found. Candidate may declare "
            "dependencies only in setup.py/pyproject.toml (not scanned "
            "by this check), or via a non-pip package manager.",
        )

    def is_own_code(f):
        resolved = f.resolve()
        return not any(
            resolved == root or root in resolved.parents
            for root in vendored_roots
        )

    # package_name -> {version -> [files that pin it there]}
    pins: dict[str, dict[str, list[Path]]] = {}
    for f in req_files:
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        for line in text.splitlines():
            match = pin_pattern.match(line)
            if not match:
                continue
            name, version = match.group(1).lower(), match.group(2)
            pins.setdefault(name, {}).setdefault(version, []).append(f)

    conflicts = {
        name: versions
        for name, versions in pins.items()
        if len(versions) > 1
    }

    labeled_files = [
        label_evidence(f, repo_path, vendored_roots) for f in req_files
    ]

    if len(req_files) == 1 and not conflicts:
        return CheckResult(
            "Dependency file conflicts", "PASS",
            f"Exactly one requirements file found "
            f"({labeled_files[0]}), no internal conflicts to check "
            f"across files.",
        )

    if conflicts:
        evidence = list(labeled_files)
        mixed_conflict = False
        for name, versions in conflicts.items():
            files_by_version = "; ".join(
                f"{version} in "
                + ", ".join(label_evidence(f, repo_path, vendored_roots) for f in files)
                for version, files in versions.items()
            )
            spans_own_and_vendored = (
                any(is_own_code(f) for files in versions.values() for f in files)
                and any(not is_own_code(f) for files in versions.values() for f in files)
            )
            if spans_own_and_vendored:
                mixed_conflict = True
                evidence.append(f"{name} [OWN CODE vs VENDORED]: {files_by_version}")
            else:
                evidence.append(f"{name}: {files_by_version}")

        detail = (
            f"{len(req_files)} requirements file(s) found, and "
            f"{len(conflicts)} package(s) are pinned to genuinely "
            f"different exact versions across them. pip will pick one "
            f"file's version depending on install order -- decide which "
            f"file is authoritative before building, not after a "
            f"confusing version-mismatch failure."
        )
        if mixed_conflict:
            detail += (
                " At least one conflict spans the candidate's OWN "
                "requirements file and a vendored one -- that's the "
                "higher-stakes case: it can affect a package your "
                "adapter's own code presumably imports, not just an "
                "unrelated bundled tool."
            )
        return CheckResult(
            "Dependency file conflicts", "FAIL", detail, evidence=evidence,
        )

    return CheckResult(
        "Dependency file conflicts", "WARN",
        f"{len(req_files)} requirements files found with no conflicting "
        f"exact pins between them. Still worth confirming which one your "
        f"adapter's Dockerfile should actually install from -- multiple "
        f"files existing at all is often a sign of a dev/prod split or "
        f"partially-abandoned dependency management.",
        evidence=labeled_files,
    )


def check_wildcard_import_risk(repo_path, vendored_roots):
    """
    Flags `from X import *` in the candidate's own Python source.

    Not a theoretical risk -- this is the exact, confirmed root cause of
    real hours-long debugging during Greenhouse onboarding.
    QemuRunner.py's `from . import *` silently pulled in every sibling
    module in backend/, including Binary.py (which drags in angr) and
    FirmAEwrapper.py (which drags in pwntools -> an incompatible
    pyelftools version) -- neither of which QemuRunner's own logic
    (the only thing run_unpack/run_emulate actually needed) used at
    all. The eventual fix was a one-line sed removing that single
    import line, which also let two large, unrelated dependency chains
    be dropped from the Dockerfile entirely.

    A wildcard import doesn't guarantee this problem, but it's cheap to
    grep for and expensive to discover by hand mid-build -- worth
    knowing about before writing a single line of adapter code, not
    after a confusing stack trace three modules deep in a library the
    adapter never needed.
    """
    pattern = re.compile(r"^\s*from\s+[\w.]*\s+import\s+\*\s*$")
    hits = []
    for f in repo_path.rglob("*.py"):
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if pattern.match(line):
                hits.append(
                    f"{label_evidence(f, repo_path, vendored_roots)}:"
                    f"{line_no}: {line.strip()}"
                )
    if not hits:
        return CheckResult(
            "Wildcard import risk", "PASS",
            "No 'from X import *' found in the candidate's Python source.",
        )
    return CheckResult(
        "Wildcard import risk", "WARN",
        f"Found {len(hits)} wildcard import(s). This is exactly the "
        f"pattern that caused real, hours-long dependency-chain "
        f"breakage onboarding Greenhouse (a wildcard import in "
        f"QemuRunner.py silently pulled in angr and pwntools, neither "
        f"actually needed by the code path being adapted). Before "
        f"installing everything this module's package imports "
        f"transitively, check whether the specific function(s) your "
        f"adapter calls actually need all of it -- if not, the fix is "
        f"often a one-line patch removing the wildcard import, the "
        f"same convention already used in adapters/firmadyne's and "
        f"adapters/firmae's committed patches.",
        evidence=hits[:15],
    )


def check_external_download_links(repo_path, vendored_roots):
    """
    Enumerates hardcoded download URLs across bootstrap scripts,
    Dockerfiles, and requirements files, and surfaces the actual URLs --
    not just a count.

    Grounded in a real, published finding, not a guess: a USC/ISI study
    of cybersecurity research artifacts (isi.edu, "Even Verified
    Cybersecurity Research Artifacts Can Be Hard to Reuse") found
    "broken links, missing components, specialized resource
    requirements, inconsistent packaging, incomplete documentation and
    evolving software dependencies" among the concrete barriers to
    reusing badged, peer-reviewed artifacts. Link rot is real and
    common enough in this exact space to be worth surfacing explicitly,
    not buried as a bare count inside check_bootstrap_scripts.

    This can't check whether a URL is actually still reachable --
    genuinely verifying that would mean making real network requests
    during static assessment, out of scope here. What it gives instead
    is the concrete list worth spot-checking by hand (or scripting a
    HEAD request against) before trusting an automated Docker build to
    succeed unattended.
    """
    url_pattern = re.compile(r"https?://[^\s\"'\)]+")
    source_names = {
        "install.sh", "setup.sh", "bootstrap.sh", "init.sh",
        "configure", "download.sh", "Dockerfile", "requirements.txt",
    }
    candidates = [
        f for f in repo_path.rglob("*")
        if f.is_file() and (
            f.name in source_names or f.name.endswith(".dockerfile")
        )
    ]

    evidence = []
    total_urls = 0
    for f in sorted(candidates):
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        urls = url_pattern.findall(text)
        if not urls:
            continue
        total_urls += len(urls)
        label = label_evidence(f, repo_path, vendored_roots)
        for url in urls[:10]:  # cap per-file to keep the report readable
            evidence.append(f"{label}: {url}")

    if not total_urls:
        return CheckResult(
            "External download links", "PASS",
            "No hardcoded http(s):// URLs found in install scripts, "
            "Dockerfiles, or requirements files.",
        )

    return CheckResult(
        "External download links", "WARN",
        f"{total_urls} hardcoded download URL(s) found across install "
        f"scripts, Dockerfile(s), and requirements files. None of these "
        f"were checked for reachability -- that needs a real network "
        f"request, out of scope for a static scan. Spot-check the ones "
        f"below by hand (a moved GitHub release, a decommissioned "
        f"mirror, or an expired domain are all real, documented, common "
        f"reasons research software builds break over time, not "
        f"hypothetical edge cases) before relying on an unattended "
        f"Docker build.",
        evidence=evidence,
    )


def check_native_build_toolchain(repo_path, vendored_roots):
    """
    Flags signals that the candidate needs to COMPILE something from
    source -- Rust, C/C++ via CMake, or a Python C-extension -- which a
    naive Dockerfile can silently fail to provision even after
    correctly installing every *runtime* dependency.

    Grounded in a real, published finding: an artifact-evaluation
    failure-analysis study (arxiv 2602.02235) found environment/
    dependency issues were the dominant failure category, and named
    "unavailable toolchains in containerized settings" specifically --
    distinct from missing Python packages, which
    check_dependency_file_conflicts and check_python_version_compatibility
    already cover. A `pip install` that needs to compile a C extension,
    or a `cargo build`, fails with a real but often confusing error
    (missing headers, missing linker, missing `cc`) if the base image
    only has a Python/apt runtime and never installed build-essential,
    a Rust toolchain, or cmake.

    Static and heuristic, same caveat as every other check here: a
    match means "go verify your Dockerfile actually installs this",
    not "this candidate is broken".
    """
    signals: dict[str, list[Path]] = {}

    for f in repo_path.rglob("Cargo.toml"):
        signals.setdefault(
            "Rust (Cargo.toml found -- needs a Rust toolchain, e.g. "
            "via rustup, not just apt)",
            [],
        ).append(f)

    for f in list(repo_path.rglob("CMakeLists.txt")):
        signals.setdefault(
            "CMake/C++ (CMakeLists.txt found -- needs cmake + a C/C++ "
            "compiler, e.g. build-essential)",
            [],
        ).append(f)

    setup_py_ext_pattern = re.compile(r"ext_modules\s*=|Extension\(")
    for f in repo_path.rglob("setup.py"):
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        if setup_py_ext_pattern.search(text):
            signals.setdefault(
                "Python C extension (setup.py declares ext_modules -- "
                "needs a C compiler and Python dev headers, e.g. "
                "build-essential + python3-dev)",
                [],
            ).append(f)

    if not signals:
        return CheckResult(
            "Native build toolchain", "PASS",
            "No Cargo.toml, CMakeLists.txt, or setup.py-declared C "
            "extensions found -- no signal that a native compiler "
            "toolchain is needed beyond what a plain Python/apt "
            "runtime image provides.",
        )

    evidence = []
    for description, files in signals.items():
        for f in sorted(files):
            evidence.append(
                f"{label_evidence(f, repo_path, vendored_roots)}: "
                f"{description}"
            )

    return CheckResult(
        "Native build toolchain", "WARN",
        f"Found signal(s) that this candidate needs to compile "
        f"something from source, not just install pre-built packages. "
        f"Confirm your adapter's Dockerfile explicitly installs the "
        f"matching toolchain -- a base image with only python3/pip "
        f"will fail here with an error about a missing compiler or "
        f"linker, which reads like a dependency problem but is "
        f"actually a missing system package.",
        evidence=evidence,
    )


def check_bootstrap_scripts(repo_path, vendored_roots):
    """
    Inventories scripts that likely run during installation/setup and
    flags a few cheap, high-signal patterns inside them: sudo usage,
    external downloads, and nested git clones. Doesn't (can't, statically)
    tell you what files/state such a script actually creates -- that
    needs a real, sandboxed dry-run, which is future work, not this
    check. What this gives you is the list of scripts worth reading
    BEFORE assuming `pip install -r requirements.txt` is the whole
    installation story -- real candidates in this project (Greenhouse's
    own install.sh/download.sh, FirmAE's install.sh) all needed
    meaningfully more than that.
    """
    script_names = {
        "install.sh", "setup.sh", "bootstrap.sh", "init.sh",
        "configure", "download.sh",
    }
    scripts = [
        f for f in repo_path.rglob("*")
        if f.is_file() and f.name in script_names
    ]

    if not scripts:
        return CheckResult(
            "Bootstrap / install scripts", "INFO",
            "No install.sh/setup.sh/bootstrap.sh/configure/download.sh "
            "found. Candidate likely installs via requirements.txt/"
            "setup.py alone -- or its bootstrap step lives somewhere "
            "this scan doesn't look for (a Makefile target, inline "
            "Dockerfile RUN commands, etc.); worth a manual check "
            "either way.",
        )

    evidence = []
    for f in sorted(scripts):
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        signals = []
        if re.search(r"\bsudo\b", text):
            signals.append("uses sudo")
        download_count = len(re.findall(r"\b(wget|curl)\s", text))
        if download_count:
            signals.append(f"{download_count} external download(s)")
        clone_count = len(re.findall(r"\bgit\s+clone\b", text))
        if clone_count:
            signals.append(f"{clone_count} nested git clone(s)")
        signal_text = ", ".join(signals) if signals else "no notable patterns"
        evidence.append(
            f"{label_evidence(f, repo_path, vendored_roots)}: {signal_text}"
        )

    return CheckResult(
        "Bootstrap / install scripts", "INFO",
        f"{len(scripts)} bootstrap/install script(s) found. Read these "
        f"before assuming a plain pip/apt install covers everything -- "
        f"external downloads and nested clones are exactly the kind of "
        f"step an adapter's Dockerfile has to reproduce explicitly, and "
        f"they're easy to miss if you only look at the dependency "
        f"files.",
        evidence=evidence,
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
    vendored_roots = find_vendored_roots(repo_path)
    return [
        check_vendored_code(repo_path, vendored_roots),
        check_single_container_buildability(repo_path),
        check_multiservice_architecture(repo_path),
        check_bare_host_dind_assumptions(repo_path, vendored_roots),
        check_architecture_hardcoding(repo_path, vendored_roots),
        check_privileged_or_device_requirements(repo_path, vendored_roots),
        check_python_version_compatibility(repo_path, vendored_roots),
        check_dependency_file_conflicts(repo_path, vendored_roots),
        check_wildcard_import_risk(repo_path, vendored_roots),
        check_native_build_toolchain(repo_path, vendored_roots),
        check_external_download_links(repo_path, vendored_roots),
        check_bootstrap_scripts(repo_path, vendored_roots),
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
        lines.append(
            "ℹ️ **No known static blockers detected.** This means none of "
            "this script's pattern checks fired — it does NOT mean the "
            "candidate is confirmed compatible. Dependency resolution, "
            "the actual Docker build, and a real run have not been "
            "attempted. Treat this as 'no red flags from a quick static "
            "scan', not a green light."
        )
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
