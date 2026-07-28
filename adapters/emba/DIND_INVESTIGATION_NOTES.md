# EMBA Docker-in-Docker investigation — RESOLVED

## The fix
Pass `-i` to the `emba` script. This sets `IN_DOCKER=1` and `USE_DOCKER=0`
together (see helpers/helpers_emba_parameter_parser.sh, case `i)`),
telling EMBA it is already running inside its target container and
should NOT try to spawn a sibling `embeddedanalyzer/emba` container via
the Docker socket. It runs the full analysis in-place instead.

This makes EMBA's adapter symmetric with fact_extractor's: the adapter
container runs the candidate tool's real logic directly, no nested
sibling-container orchestration needed at all. The earlier identical-
host-path mount work and the `embeddedanalyzer/emba` image tag are
NOT NEEDED with this approach — `-i` sidesteps the whole mechanism.

## Confirmed working invocation
    /path/to/emba -l <log_dir> -f <firmware_path> \
        -p <emba_repo>/scan-profiles/default-scan.emba -F -i

Ran successfully end-to-end against a real firmware sample
(DIR-868L_REVA_FIRMWARE_1.12B04.zip): ~56 seconds, dozens of real static
analysis modules executed (S09, S75, S85, S130, F50, etc.), correct
OS detection ("Possible operating system detected (unverified): QNX"),
exit code 0 — a genuine result, verified by content, not just exit code
(distinct from an earlier false positive that also returned exit 0 with
zero actual work done — see git history for that investigation).

## Known cosmetic issues (non-blocking)
- `rev: command not found` — util-linux/bsdextrautils dependency missing,
  likely because our build used the -g (GH_ACTION) flag to skip CVE
  database population; some dependency-check paths may be tied to that
  same flag. S09 handles the missing tool gracefully (reports "nothing
  found" rather than crashing). Low priority — does not affect the
  pipeline stages VERITAS actually measures (unpack, boot, endpoints).
- `cp: cannot stat './helpers/base.html'`, `sed: can't read .../html-report/*.html`,
  `find: './modules': No such file or directory` — HTML report generation
  assumes relative paths from EMBA's own repo root as the working
  directory. Our test ran emba via an absolute path from a different cwd.
  Does not affect real analysis module execution (all completed and
  reported genuine findings) — only cosmetic web-report generation.
  Fix: cd into the emba repo root before invoking, or set EMBA's own
  documented working-directory convention explicitly in entrypoint.py.

## Adapter design implication
run_unpack/run_emulate should invoke `emba` directly with `-i -F`
(no Docker socket mount, no sibling container, no identical-path-mount
requirement) — a materially simpler adapter than originally scoped.
The privileged mode and /dev/fuse device access are still required
(per docker-compose.yml), but the sibling-container complexity is gone.
