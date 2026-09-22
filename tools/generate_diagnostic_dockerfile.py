#!/usr/bin/env python3
"""
tools/generate_diagnostic_dockerfile.py

Onboarding Reliability, Doctor stages 5 and 6 (staged diagnostic build
+ install self-test gate) -- the first genuinely dynamic layer of the
doctor, everything before this was static/near-static analysis.

WHY A STAGED BUILD: a single monolithic Dockerfile fails at ONE line
and gives you one opaque error. This generates a Dockerfile with
separate, independently-buildable stages (`docker build --target
<stage>`), so a failure is localized to exactly one layer --
toolchain, Python version, dependencies, source+bootstrap, or the
final self-test -- instead of "the build failed somewhere in these
40 lines."

WHY REUSE THE DOCTOR'S OWN FINDINGS: every fact this generator needs
(does it need a Rust/CMake/C toolchain, which apt packages a known
native-dependency package needs, which scripts are the real bootstrap
step, whether a Python version constraint exists) was already
discovered tonight by tools/assess_candidate.py's static checks. This
does not re-invent that detection -- it reuses find_vendored_roots()
and the SAME structural scan logic those checks already use, so this
generator and the static report can never structurally disagree about
what's actually in the candidate's own code.

WHAT THIS DOES NOT DO: it never executes anything itself. It only
WRITES a Dockerfile. Building it, and therefore actually running the
candidate's bootstrap script (stage 4's sandboxed-execution goal,
which happens as one RUN step inside the 'bootstrap' stage below --
sandboxed BY the fact that it only ever runs inside a Docker build,
never directly on the host) needs a real `docker build` on YOUR
machine. Nothing in this file has been run against Docker -- no
Docker in the environment this was written in. Signature- and logic-
checked against synthetic fixtures, syntax-valid, not yet build-tested
for real. Treat the first real `docker build` using this generator's
output as its actual test.

Usage:
    ./python tools/generate_diagnostic_dockerfile.py <candidate_path> [--output PATH]

    # Then, on a real Docker-capable machine, build stage by stage:
    docker build --target toolchain     -t diag-toolchain    -f <output> <candidate_path>
    docker build --target dependencies  -t diag-dependencies -f <output> <candidate_path>
    docker build --target bootstrap     -t diag-bootstrap    -f <output> <candidate_path>
    docker build --target selftest      -t diag-selftest     -f <output> <candidate_path>
    # Whichever target fails first tells you exactly which layer broke.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from assess_candidate import (  # noqa: E402
    find_vendored_roots,
    find_dockerfiles,
    is_ci_config_script,
)

TEMPLATE_DEFAULT_PYTHON = "3.10"  # matches adapters/_template/Dockerfile's base

BOOTSTRAP_SCRIPT_NAMES = {
    "install.sh", "setup.sh", "bootstrap.sh", "init.sh",
    "configure", "download.sh",
}

# Same curated list as check_known_native_dependency_packages in
# assess_candidate.py -- kept in sync deliberately; if that list grows,
# grow this one the same way, same reasoning applies here as there.
KNOWN_NATIVE_PACKAGES = {
    "lxml": ["libxml2-dev", "libxslt1-dev"],
    "psycopg2": ["libpq-dev"],
    "mysqlclient": ["default-libmysqlclient-dev", "build-essential", "pkg-config"],
    "pygraphviz": ["graphviz", "libgraphviz-dev", "pkg-config"],
    "python-ldap": ["libldap2-dev", "libsasl2-dev"],
    "pycurl": ["libcurl4-openssl-dev", "libssl-dev"],
    "pyaudio": ["portaudio19-dev"],
    "gdal": ["libgdal-dev"],
    "pyzmq": ["libzmq3-dev"],
}


class DiagnosticFacts:
    """Structured facts this generator needs -- gathered once, directly
    from the repo, not parsed out of another check's rendered text."""

    def __init__(self):
        self.own_dockerfile_exists = False
        self.bootstrap_scripts: list[str] = []  # relative paths, as strings
        self.needs_rust = False
        self.needs_cmake = False
        self.needs_c_ext = False
        self.native_apt_packages: set[str] = set()
        self.requirements_file: str | None = None  # relative path, as a string
        self.python_version = TEMPLATE_DEFAULT_PYTHON


def is_own_code(path: Path, vendored_roots: set[Path]) -> bool:
    resolved = path.resolve()
    return not any(
        resolved == root or root in resolved.parents
        for root in vendored_roots
    )


def gather_facts(repo_path: Path) -> DiagnosticFacts:
    vendored_roots = find_vendored_roots(repo_path)
    facts = DiagnosticFacts()

    facts.own_dockerfile_exists = any(
        is_own_code(f, vendored_roots) for f in find_dockerfiles(repo_path)
    )

    facts.bootstrap_scripts = sorted(
        str(f.relative_to(repo_path))
        for f in repo_path.rglob("*")
        if f.is_file() and f.name in BOOTSTRAP_SCRIPT_NAMES
        and is_own_code(f, vendored_roots)
        and not is_ci_config_script(f)
    )

    if any(
        is_own_code(f, vendored_roots) for f in repo_path.rglob("Cargo.toml")
    ):
        facts.needs_rust = True
    if any(
        is_own_code(f, vendored_roots) for f in repo_path.rglob("CMakeLists.txt")
    ):
        facts.needs_cmake = True

    ext_pattern = re.compile(r"ext_modules\s*=|Extension\(")
    for f in repo_path.rglob("setup.py"):
        if not is_own_code(f, vendored_roots):
            continue
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        if ext_pattern.search(text):
            facts.needs_c_ext = True

    package_pattern = re.compile(r"^\s*([A-Za-z0-9_.\-]+)", re.MULTILINE)
    own_requirements = [
        f for f in repo_path.rglob("*requirements*.txt")
        if is_own_code(f, vendored_roots)
    ]
    if len(own_requirements) == 1:
        facts.requirements_file = str(own_requirements[0].relative_to(repo_path))
        try:
            text = own_requirements[0].read_text(errors="ignore")
        except Exception:
            text = ""
        for line in text.splitlines():
            match = package_pattern.match(line)
            if not match:
                continue
            name = match.group(1).lower()
            if name.endswith(("-binary", "_binary")):
                continue
            if name in KNOWN_NATIVE_PACKAGES:
                facts.native_apt_packages.update(KNOWN_NATIVE_PACKAGES[name])

    return facts


def render_dockerfile(facts: DiagnosticFacts, candidate_name: str) -> str:
    lines = [
        f"# Diagnostic Dockerfile, auto-generated by",
        f"# tools/generate_diagnostic_dockerfile.py for: {candidate_name}",
        f"#",
        f"# NOT a real adapter Dockerfile -- for localizing WHERE a",
        f"# candidate's install actually breaks, one stage at a time:",
        f"#   docker build --target toolchain    -f <this file> <repo>",
        f"#   docker build --target dependencies -f <this file> <repo>",
        f"#   docker build --target bootstrap    -f <this file> <repo>",
        f"#   docker build --target selftest     -f <this file> <repo>",
        f"# Whichever target fails first is exactly where the real",
        f"# problem is -- no need to read through an entire build log.",
        f"",
        f"FROM ubuntu:22.04 AS base",
        f"RUN apt-get update && apt-get install -y --no-install-recommends \\",
        f"    python3 python3-pip python3-venv git ca-certificates curl \\",
        f" && rm -rf /var/lib/apt/lists/*",
        f"",
    ]

    lines.append("FROM base AS toolchain")
    toolchain_packages = ["build-essential"] if (
        facts.needs_cmake or facts.needs_c_ext or facts.native_apt_packages
    ) else []
    if facts.needs_cmake:
        toolchain_packages.append("cmake")
    toolchain_packages.extend(sorted(facts.native_apt_packages))
    if toolchain_packages:
        pkg_list = " \\\n    ".join(sorted(set(toolchain_packages)))
        lines.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            f"    {pkg_list} \\\n"
            " && rm -rf /var/lib/apt/lists/*"
        )
    else:
        lines.append("# No native toolchain or system C library signals detected.")
    if facts.needs_rust:
        lines.append(
            "RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs "
            "| sh -s -- -y"
        )
        lines.append('ENV PATH="/root/.cargo/bin:${PATH}"')
    lines.append("")

    lines.append("FROM toolchain AS dependencies")
    if facts.requirements_file is not None:
        # COPY's source path must be the real relative path from the
        # build context root -- at THIS stage, only this one file has
        # been copied in yet (COPY . /candidate happens later, in the
        # 'source' stage), so a nested file (e.g. pyplugins/requirements.txt,
        # confirmed real for PENGUIN tonight) needs its real subpath here,
        # not just its bare filename.
        lines.append(f"COPY {facts.requirements_file} /tmp/requirements.txt")
        lines.append(
            "# NOTE: only the single unambiguous own-code requirements.txt "
            "this generator found is copied here -- if the real candidate "
            "has more than one, or declares deps only in setup.py/"
            "pyproject.toml, this stage under-represents the real "
            "dependency surface. Check the static report's 'Dependency "
            "file conflicts' finding before trusting this stage alone."
        )
        lines.append("RUN pip3 install --no-cache-dir -r /tmp/requirements.txt")
    else:
        lines.append(
            "# No single unambiguous own-code requirements.txt found -- "
            "this generator can't know what to pip install here. Fill "
            "this stage in by hand, or fix ambiguity first."
        )
    lines.append("")

    lines.append("FROM dependencies AS source")
    lines.append("COPY . /candidate")
    lines.append("WORKDIR /candidate")
    lines.append("")

    lines.append("FROM source AS bootstrap")
    if facts.bootstrap_scripts:
        for script_relpath in facts.bootstrap_scripts:
            lines.append(
                f"# This is Doctor stage 4 (sandboxed bootstrap capture) --"
            )
            lines.append(
                f"# genuinely sandboxed BECAUSE this only ever runs inside "
                f"a Docker build, never directly on a host."
            )
            lines.append(
                f'RUN echo "--- before bootstrap ---" && find /candidate '
                f'-maxdepth 2 > /tmp/before.txt'
            )
            # Real relative path (from WORKDIR /candidate, which matches
            # the repo root exactly since COPY . /candidate above copied
            # everything) -- NOT just the script's bare filename, which
            # would break for anything not sitting at the repo root.
            lines.append(
                f"RUN bash {script_relpath} || "
                f"(echo 'BOOTSTRAP SCRIPT FAILED: {script_relpath}' && exit 1)"
            )
            lines.append(
                f'RUN echo "--- after bootstrap ---" && find /candidate '
                f'-maxdepth 2 > /tmp/after.txt && diff /tmp/before.txt '
                f'/tmp/after.txt || true'
            )
    else:
        lines.append(
            "# No bootstrap script detected by this generator's scan -- "
            "if the candidate has one under a different name (a Makefile "
            "target, an inline step in its own README), it's not "
            "represented here."
        )
    lines.append("")

    lines.append("FROM bootstrap AS selftest")
    lines.append(
        "# Doctor stage 6 (install self-test / gate) -- deliberately "
        "minimal: this generator has no reliable way to know the "
        "candidate's real entry point or importable module name. Fill "
        "in a real check by hand (e.g. RUN python3 -c \"import "
        "<real_module>\", or RUN <candidate's real CLI> --help) before "
        "trusting this stage as a genuine pass/fail gate."
    )
    lines.append('RUN echo "Reached selftest stage -- fill in a real check above."')
    lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a staged diagnostic Dockerfile from the "
        "Candidate Doctor's own structural findings.",
    )
    parser.add_argument("candidate_path", help="Local path to the candidate's repo")
    parser.add_argument(
        "--output", default=None,
        help="Where to write the Dockerfile (default: <candidate>/Dockerfile.diagnostic)",
    )
    args = parser.parse_args()

    repo_path = Path(args.candidate_path).resolve()
    if not repo_path.is_dir():
        print(f"FATAL: path does not exist: {repo_path}", file=sys.stderr)
        return 1

    facts = gather_facts(repo_path)
    dockerfile_text = render_dockerfile(facts, repo_path.name)

    output_path = Path(args.output) if args.output else repo_path / "Dockerfile.diagnostic"
    output_path.write_text(dockerfile_text)

    print(f"Wrote diagnostic Dockerfile: {output_path}")
    print()
    print("Facts this was generated from:")
    print(f"  bootstrap scripts found : {facts.bootstrap_scripts or 'none'}")
    print(f"  needs Rust toolchain    : {facts.needs_rust}")
    print(f"  needs CMake/C++         : {facts.needs_cmake}")
    print(f"  needs C extension build : {facts.needs_c_ext}")
    print(f"  native apt packages     : {sorted(facts.native_apt_packages) or 'none'}")
    print(f"  requirements.txt used   : {facts.requirements_file or 'none (ambiguous or absent)'}")
    print()
    print("Next, on a real Docker machine, build stage by stage:")
    print(f"  docker build --target toolchain    -f {output_path} {repo_path}")
    print(f"  docker build --target dependencies -f {output_path} {repo_path}")
    print(f"  docker build --target bootstrap    -f {output_path} {repo_path}")
    print(f"  docker build --target selftest     -f {output_path} {repo_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
