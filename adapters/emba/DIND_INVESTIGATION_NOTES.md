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
  pipeline stages FIRMARBITER actually measures (unpack, boot, endpoints).
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

---

## NEW FINDING (separate from DinD): binwalk/unblob/rev missing from final image

Confirmed via direct check inside firmarbiter-adapter-emba:0.1.0:
    which binwalk unblob rev
    -> all three return nothing (not installed / not on PATH)

Consequence: EMBA's static extraction modules (P50_binwalk_extractor,
P55_unblob_extractor, P60_deep_extractor) all produced empty output
against two different real firmware samples (DIR-868L REVA and REVB),
both reporting only "1 files and 2 directories detected" — consistent
with extraction tools being absent, not a firmware-specific issue.

Install log DOES show sasquatch and other IP61_unblob.sh dependencies
being installed ("sasquatch will be newly installed") — so either:
  (a) the actual binwalk/unblob binary install step is being skipped
      by a condition we haven't traced (possibly related to -g/GH_ACTION,
      since IP61_unblob.sh's condition checks LIST_DEP/IN_DOCKER/
      DOCKER_SETUP/FULL, not GH_ACTION directly, so this needs verifying)
  (b) the binaries install into a Python venv or path not on $PATH in
      the final running container
  (c) something in our patches/arm64-compat.patch inadvertently affects
      this despite being scoped to architecture-hardcoding + -g only

NEXT SESSION: 
1. Rebuild WITHOUT -g flag (accept the slow CVE database step) and
   recheck `which binwalk unblob` — isolates whether -g is the cause
2. If still missing, grep installer/IP61_unblob.sh and IP99_binwalk_default.sh
   for the actual binary install commands and trace exactly what runs
3. Check if PATH includes wherever pip/venv might install these tools
   (some Python-based unpackers install as pip packages, not system binaries)

---

## MAJOR CORRECTION: wrong architecture assumed for the entire build approach

Confirmed via EMBA's own wiki (https://github.com/e-m-b-a/emba/wiki/Installation,
https://github.com/e-m-b-a/emba/wiki/FAQ):

"The standard Docker variant only requires Docker and cve-search to be
installed on the HOST (sudo ./installer.sh -d) and EMBA will install
everything else in the Docker container itself DURING ITS FIRST RUN."

"WARNING: Do not use EMBA in developer mode (-D) as it can execute
malicious code... and harm your host system!"

This means:
- -D is explicitly a DEVELOPER-ONLY mode, not a path to a complete image.
  We used it in every build attempt tonight and in the prior session.
- installer.sh -d is meant to run on the BARE HOST (not inside a Dockerfile
  RUN step) — it installs Docker + cve-search on the host only.
- The actual tool population (binwalk, unblob, etc.) happens LAZILY,
  automatically, inside the running container on its first real analysis
  invocation — NOT baked into the image at build time via installer.sh.

This explains every finding from tonight's investigation:
- binwalk/unblob/rev missing: never a bug, they were never supposed to be
  present after a `docker build` — they install on first `emba` run
- The DOCKER_SETUP/-D/-g flag conflicts: irrelevant, because -D was never
  the right flag to be using for a Dockerfile RUN step in the first place
- "Docker daemon not running" error under -s -g: I05_emba_docker_image_dl
  tries to check/pull the real embeddedanalyzer/emba image, which requires
  an actual running Docker daemon — something that doesn't exist inside a
  `docker build` sandbox at all, confirming this install path was never
  meant to run inside a Dockerfile build step.

## Corrected next-session plan (NOT YET ATTEMPTED)
1. Do NOT bake EMBA's tools into the image at build time via installer.sh.
2. Build a MINIMAL Dockerfile: just clone EMBA + apply the 3 ARM64 patches
   (sasquatch/libfuse2t64/uml-utilities still needed since those errors
   were REAL, confirmed independently of the -D/-g confusion).
3. Test whether tools genuinely self-install on first `emba -f ... -i`
   invocation, inside a container, matching -d's documented lazy-install
   behavior — this has NOT been tested yet, it's a real open question.
4. If lazy install requires internet access at analysis-time (likely,
   given it downloads binwalk/unblob), this has real implications for
   the FIRMARBITER adapter contract — a "run once per firmware sample" model
   may need network access per-run, not just at build time. This is a
   genuinely new architectural question for the adapter design, not
   solved by anything tried so far.

---

## FINAL RESOLUTION: use the official embeddedanalyzer/emba:2.0.2b image directly

Confirmed via a full, successful real-firmware run (DIR-868L REVB, 13h05m
total runtime under QEMU amd64-on-arm64 translation):

- Do NOT build from source via installer.sh at all. Pull the maintainers'
  own published image directly:
      docker pull --platform linux/amd64 embeddedanalyzer/emba:2.0.2b
  (35.9GB — matches EMBA's own documented disk sizing. No ARM64 manifest
  exists, so --platform linux/amd64 is required; runs via QEMU translation,
  same accepted tradeoff as fact_extractor.)

- /emba is empty in this image too — EMBA's own script/module source must
  still be mounted at runtime from a real host-side clone (matching
  docker-compose.yml's own convention), NOT baked into this image.

- Confirmed working invocation:
    docker run --rm --privileged --device /dev/fuse \
      --memory="5g" --memory-swap="6g" \
      -v <host_emba_clone>:/emba:ro \
      -v <firmware_dir>:/firmware_samples:ro \
      -v <log_dir>:<log_dir> \
      --entrypoint /bin/bash embeddedanalyzer/emba:2.0.2b \
      -c "/emba/emba -l <log_dir> -f <firmware_path> \
          -p /emba/scan-profiles/default-scan.emba -F -i"

- CRITICAL: explicit --memory limit is required. Without one, EMBA's
  static analysis (S13/S16/S17 in particular) can consume enough RAM to
  trigger the HOST VM's OOM killer, not just the container's — this
  crashed the entire VM once during testing, requiring a reboot. With
  --memory="5g" set, only the container gets reaped if it exceeds the
  cap, leaving the VM and other services (e.g. Wazuh) untouched.

- Real timing: full default-scan.emba profile took ~13 hours against a
  real router firmware (DIR-868L REVB) under QEMU translation. The two
  slowest modules were S109_jtr_local_pw_cracking (~2h) and
  S115_usermode_emulator (~1.5h). This MUST be documented as an accepted,
  expected cost in adapter.yaml — not a bug, not something to optimize
  away, consistent with the amd64-under-QEMU compute-cost caveat already
  applied to FirmAE/FIRMADYNE.

- Real extraction output path pattern (confirmed against real firmware):
    firmware/binwalk_extracted/<firmware_filename>.extracted/0/
      <inner_filename>.extracted/<hex_offset>/squashfs-root/
  This path is NOT fixed/predictable (hex offset varies per firmware).
  run_unpack must search recursively under firmware/binwalk_extracted/
  for a directory matching common rootfs markers (e.g. containing both
  an 'etc' and 'bin' subdirectory) rather than assume a fixed path.

- Real result quality confirmed: 30 SBOM components identified with
  versions, 4059 CVE entries matched (78 critical, 701 high), 140
  possible exploits (11 Metasploit modules) — this image ships with a
  populated CVE database already (unlike a from-scratch installer.sh
  build), so F17_cve_bin_tool produces real, substantive results.
