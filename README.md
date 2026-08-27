# FirmArbiter

FirmArbiter is a candidate-neutral benchmark and evaluation protocol for Linux
firmware rehosting and analysis tools. It does not trust a tool's own
self-reported success — every measurement (did the firmware unpack? did it
boot? is a service genuinely reachable? is it stable?) is checked
independently by FirmArbiter itself, using direct evidence (real extracted
files, real console output, a real network probe), not the candidate's word
for it.

This document takes you from a fresh clone all the way to a real, completed
comparison run across multiple candidate tools. Follow it top to bottom on a
clean machine.

---

## 1. What you actually need before starting

### 1.1 Operating system — read this honestly before choosing a machine

**FirmArbiter is built and tested on Linux (Ubuntu).** The candidate tools it
wraps (FirmAE, FIRMADYNE) genuinely rehost and emulate real firmware — this
requires real Linux kernel features inside the container: loop devices,
device-mapper, TUN/TAP virtual networking, and `NET_ADMIN` capabilities,
declared explicitly in every adapter's manifest as `requirements:
[loop-devices, device-mapper, tun-tap, net-admin, full-privileged]`.

| Platform | Status |
|---|---|
| **Native Linux** (Ubuntu 22.04+ recommended) | ✅ Fully supported, this is what's actually been tested |
| **Windows, via WSL2** | ⚠️ Likely to work since WSL2 runs a real Linux kernel, but **not personally verified by this project**. If you try it, do so expecting to troubleshoot, and please report back what you find. |
| **macOS, native (no VM)** | ⚠️ Likely to work for the same reason as WSL2 (Docker Desktop on Mac also runs a real Linux VM underneath), **not personally verified**. |
| **Windows/macOS, via Docker Desktop directly (no WSL2/Linux VM)** | ❓ Genuinely uncertain. Docker Desktop's own virtualization layer may or may not correctly pass through the privileged networking/device features FirmAE and FIRMADYNE need. Do not assume this works — test it early with the small confidence-check in section 4 before investing time in a full run. |

**If you're not on native Linux and want the most reliable path:** use a
Linux virtual machine (UTM on Apple Silicon Mac, VirtualBox/Hyper-V/VMware on
Windows, or a cloud VM) rather than relying on Docker Desktop's own
virtualization. This project itself was built and run entirely inside a
Ubuntu VM.

### 1.2 Hardware requirements — real numbers, not guesses

These are based on actual measured usage during this project's development,
not estimates:

| Resource | Minimum | Recommended | Why |
|---|---|---|---|
| **RAM** | 8 GB | 16 GB+ | See §5.5 — the per-candidate memory cap is a real, user-adjustable flag (`--memory-gb`), not a fixed limit. The figures here describe the minimum needed to run comfortably at the conservative default. |
| **Disk space** | 80 GB free | 150 GB+ free | EMBA alone can produce **20+ GB of output for a single firmware image** (extracted filesystem, Ghidra decompilation projects, CVE database matching artifacts). Running EMBA against even a handful of firmware images without cleaning up between runs will exhaust a small disk fast. |
| **CPU** | 4 cores | 8+ cores | Static analysis (EMBA) and dynamic emulation (FirmAE/FIRMADYNE) are both genuinely CPU-heavy. More cores means faster runs, not just more headroom — see §5.5. |

### 1.3 Software prerequisites

- **Docker** (Docker Engine on Linux, or Docker Desktop on macOS/Windows),
  with the ability to run **privileged containers**. Confirm Docker is
  installed and working:
```bash
  docker --version
  docker run --rm hello-world
```
- **Python 3.10+**
```bash
  python3 --version
```
- **Git**
```bash
  git --version
```
- **`sudo`/administrator access** — several steps (fixing file ownership
  left behind by containers that run as root internally) require it.

**If you're on Apple Silicon (M1/M2/M3/M4) or any ARM64 host:** every
candidate tool's Docker image is `linux/amd64`-only. Docker will
transparently run these through QEMU emulation, which works, but is
noticeably slower and less predictable than running natively — expect longer
runtimes than the figures quoted later in this document, and do not use
timing/compute-cost figures gathered on an ARM64 host as final performance
numbers for any comparative writeup.

---

## 2. Getting the code

```bash
git clone <YOUR_REPOSITORY_URL_HERE> firmarbiter
cd firmarbiter
git checkout refactor/adapter-contract-v1
```

> Replace `<YOUR_REPOSITORY_URL_HERE>` with your actual repository URL. If
> you're reading this from inside an already-cloned copy, you can skip this
> step — just make sure you're on the right branch:
> ```bash
> git branch --show-current
> ```

---

## 3. Understanding what you just cloned (a 60-second orientation)

**Important: everything under `adapters/` is source code, not built
images.** Cloning the repo does not give you working Docker images for any
candidate — it gives you the Dockerfiles, Python entrypoints, and manifests
needed to *build* them. No image exists until you either build one manually
(§5) or run an experiment and let the coordinator build it automatically.

```
firmarbiter/
├── run_firmarbiter.py          # the CLI you'll actually run
├── firmarbiter_core/           # the coordinator itself — orchestration,
│                                # independent verification probes, event
│                                # schema validation
│   └── probes/                 # the code that independently verifies
│                                # unpack success, boot success, etc. —
│                                # never trusts a candidate's self-report
├── adapters/                   # SOURCE CODE for one candidate tool per
│                                # folder — Dockerfile + entrypoint.py +
│                                # adapter.yaml, not built images
│   ├── firmae/
│   ├── firmadyne/
│   ├── emba/
│   ├── fact_extractor/         # known-incomplete, see §6.4
│   └── _template/               # copy this to start building a NEW
│                                # adapter for a tool that isn't here yet
│                                # — see §12
├── schemas/                    # the real, enforced JSON schemas every
│                                # adapter manifest and event must satisfy
├── score_aggregator.py         # turns raw results into a summary table
├── tools/
│   └── assess_candidate.py     # pre-flight compatibility checker — run
│                                # this FIRST, before writing any new
│                                # adapter code, see §12.1
└── results/                    # every run's real output lands here,
                                  # nothing here until you run something
```

You don't need to understand the internals to run an experiment against an
existing candidate — this orientation is here so the file paths referenced
later make sense. If you're planning to add a *new* candidate tool, read
§12 as well before diving in.

---

## 4. Fast confidence check (do this before anything else)

Before building all four candidates and committing real time, confirm Docker
can actually run a privileged container correctly on your machine:

```bash
docker run --rm --privileged --platform linux/amd64 ubuntu:22.04 \
  bash -c "mount -t tmpfs tmpfs /mnt && echo 'privileged mode: OK' && umount /mnt"
```

If this prints `privileged mode: OK`, your Docker setup can do the kind of
thing FirmAE/FIRMADYNE need. If it errors out, stop here and fix your Docker
setup (or switch to a genuine Linux VM per §1.1) before continuing —
everything downstream depends on this working.

---

## 5. Building the candidate adapters — and an important thing to know first

**You likely don't need to manually build anything.** FirmArbiter's own
coordinator automatically builds each candidate's Docker image itself,
fresh, every time you launch an experiment (`docker build --pull ...`).
You can skip straight to §7 and let it build what it needs on first use.

**Manually pre-building is still worth doing once, though — as a fast,
isolated diagnostic.** If a candidate's build has a real problem (a broken
Dockerfile, a network issue reaching its source repository), you'll find
out in a focused way here, rather than as a confusing failure buried inside
a longer experiment run.

**Important: don't copy the version numbers below blindly.** The exact tag
(`firmarbiter-adapter-<name>:<version>`) is not an arbitrary convention —
it's constructed by the coordinator itself from each adapter's own
`adapter.yaml` manifest (`adapter.id` + `adapter.version`), and the
coordinator looks for an image matching that exact tag. If an adapter's
version is ever bumped, a hardcoded number here would go stale. **Before
building, check the real, current version directly from the source of
truth:**

```bash
cat adapters/emba/adapter.yaml | grep -A2 "^adapter:"
```

(swap `emba` for `firmadyne`, `firmae`, etc.) — use whatever `id:` and
`version:` that actually shows, not the numbers below if they ever differ.

Build these one at a time and confirm each succeeds:

> **On Apple Silicon / ARM64 hosts**, the `--platform linux/amd64` flag
> below is required. **On native Intel/AMD64 hosts (most Windows PCs, Intel
> Macs, most Linux servers)**, it's technically optional but harmless to
> include — it makes explicit that these images are amd64-only.

### 5.1 FirmAE

*(version number below assumed current at time of writing — verify with the command above)*
```bash
docker build --platform linux/amd64 -t firmarbiter-adapter-firmae:0.2.4 adapters/firmae/
```
Expect this to take several minutes on first build.

### 5.2 FIRMADYNE

*(version number below assumed current at time of writing — verify with the command above)*
```bash
docker build --platform linux/amd64 -t firmarbiter-adapter-firmadyne:0.1.1 adapters/firmadyne/
```
This one clones and builds FIRMADYNE's own toolchain from source — expect
this to be the longest of the three lighter builds, several minutes.

### 5.3 The neutral network probe

This is FirmArbiter's own independent reachability-checking tool — not a
candidate, but required for every run. **Its build context is different from
the other adapters** — it must be built from the `firmarbiter_core/`
directory, not from inside `probe_image/` itself:

```bash
docker build --platform linux/amd64 \
  -t firmarbiter-neutral-network-probe:1.0.0 \
  -f firmarbiter_core/probe_image/Dockerfile \
  firmarbiter_core/
```

### 5.4 EMBA

*(version number below assumed current at time of writing — verify with the command above)*
```bash
docker build --platform linux/amd64 -t firmarbiter-adapter-emba:0.3.0 adapters/emba/
```

**This is the largest build** (the resulting image is ~10 GB) and can take
10+ minutes. This is expected, not a hang.

### 5.5 If you're on a genuinely powerful machine — tune these flags upward

`--memory-gb` and `--cpu-cores` are real, adjustable CLI flags on every run
(shown in §8.3). **The `4 GB` figure quoted earlier in this document is a
conservative default calibrated for a resource-constrained development
environment, not a ceiling built into the program.** If you have a real
16 GB or 32 GB machine, raising these is genuinely worth doing, for a
concrete reason: EMBA's Ghidra decompilation stage runs *multiple
concurrent analysis processes in parallel* — during this project's own
testing, 8+ simultaneous Ghidra instances were observed running at once. A
low memory cap forces that parallel work to queue and throttle; a higher
cap on real hardware with the RAM to back it lets more of that genuine
parallel work happen simultaneously, which can meaningfully shorten
completion time, not just consume more RAM for no reason.

As a starting point on a real 16GB+ machine:
```bash
--memory-gb 10 --cpu-cores 8
```
adjusting further based on what else is running on the same machine and how
much you're comfortable leaving unused.

### 5.6 Confirm everything built correctly (if you did the manual build in §5.1–5.4)

```bash
docker images | grep firmarbiter
```

You should see all four images listed — but check the *real* versions
against each `adapter.yaml` (§5, above) rather than assuming they must
match the numbers quoted earlier in this document:
- `firmarbiter-adapter-firmae:<version from adapters/firmae/adapter.yaml>`
- `firmarbiter-adapter-firmadyne:<version from adapters/firmadyne/adapter.yaml>`
- `firmarbiter-neutral-network-probe:1.0.0`
- `firmarbiter-adapter-emba:<version from adapters/emba/adapter.yaml>`

### 5.7 Ask FirmArbiter to confirm it recognizes them

```bash
python3 run_firmarbiter.py --list-candidates
```

This validates every adapter's manifest against the real schema and reports
each candidate's declared capabilities. If this errors out for any
candidate, **stop here** — something about that adapter's manifest or build
didn't land correctly, and running an actual experiment against a
misconfigured adapter will just waste time.

---

## 6. Understanding what each candidate can actually do

This matters — **not every candidate supports every stage**, and asking for
a stage a candidate doesn't support will fail the run, not silently skip it.

| Candidate | `unpack` | `emulate` | `endpoint-discovery` | Real typical runtime |
|---|---|---|---|---|
| **FirmAE** | ✅ | ✅ | ✅ | A few minutes total, on native hardware |
| **FIRMADYNE** | ✅ | ✅ | ✅ | A few minutes total, on native hardware |
| **EMBA** | ✅ (static analysis only) | ❌ | ❌ | **13–20+ hours**, real, measured variance — see §6.3 |
| **fact_extractor** | Partially — see §6.4 | ❌ | ❌ | N/A, not currently fully functional |

### 6.1 What this means practically

If you run all candidates together with the default full stage list, **EMBA
will fail immediately** with an error like `Run request contains unsupported
stages: emulate, endpoint-discovery` — this is FirmArbiter correctly
refusing to ask EMBA to do something it can't, not a bug. **When including
EMBA in a run, always explicitly limit its stages**, either by running it in
its own separate invocation with `--stages unpack`, or by accepting that a
mixed-candidate batch including EMBA needs EMBA run separately from the
others (see §8.3 for exactly how).

### 6.2 Why EMBA takes so long

EMBA's "unpack" stage isn't just extraction — it's EMBA's **entire static
analysis pipeline** (extraction, binary protection checks, decompilation via
Ghidra, CVE database matching, SBOM generation, and full HTML reporting) all
running under a single stage name. This is a real, expected characteristic
of the tool, not something wrong with the adapter.

### 6.3 A real, honest caveat about EMBA's runtime

Runtime for the *identical* firmware image, on the *identical* hardware, has
been observed to genuinely vary — 13 hours one run, over 20 hours another —
during this project's own testing. Do not assume a fixed runtime; **always
launch EMBA with a generous `--timeout`** (see §8), and expect to check in
on long runs rather than assume a fixed completion time.

### 6.4 fact_extractor — known, currently unresolved limitation

`fact_extractor` requires launching its own analysis engine as a *sibling*
container via a mounted Docker socket (`nested-containers` in its manifest).
FirmArbiter's current coordinator does not yet support this pattern — trying
to use `fact_extractor` will fail with an explicit error naming this gap.
**Leave it out of your `--candidates` list** until this is resolved upstream.

---

## 7. Your first, fast real run

Before attempting a full multi-candidate comparison, run one fast, real
confidence-building experiment. FirmAE typically completes in a few minutes.

### 7.1 Get a real firmware image to test with

You need at least one real Linux-based firmware image (a router firmware
`.zip`/`.bin` file is a good, simple starting point). This project has used
D-Link firmware images during development; any similar consumer router
firmware works.

### 7.2 Run it

```bash
python3 run_firmarbiter.py \
  --firmware /path/to/your/firmware.zip \
  --candidates firmae \
  --experiment-id my-first-run
```

This uses the real default stages (`unpack,emulate,endpoint-discovery`) and
default timeout (3 hours — far more than FirmAE actually needs).

### 7.3 Watch it happen

The coordinator prints live progress to your terminal as each stage
completes. When it finishes, you'll see a summary line similar to this
(exact wording may vary):

```
FIRMARBITER Result: status=completed unpack=true boot=true reachable=true
FIRMARBITER Saved to: results/runs/my-first-run.<case-id>.firmae.attempt-1/final-result.json
```

### 7.4 Look at the real result

```bash
cat results/runs/my-first-run.*firmae*/final-result.json | python3 -m json.tool
```

Look specifically at `independent_measurements` — this is FirmArbiter's own,
independently-verified view of what actually happened, separate from
anything the candidate tool itself claimed.

**If this completed with `unpack=true`, you're genuinely ready to move on.**

---

## 8. Running a real multi-candidate comparison

### 8.1 The simple case — candidates that all support the full stage list

```bash
python3 run_firmarbiter.py \
  --firmware /path/to/your/firmware.zip \
  --candidates firmae,firmadyne \
  --experiment-id my-comparison
```

Both will run the full `unpack,emulate,endpoint-discovery` pipeline and
should each complete within several minutes.

### 8.2 Adding EMBA to the picture — run it separately

Because EMBA only supports `unpack` (§6.1), give it its own invocation, with
a genuinely generous timeout:

```bash
python3 run_firmarbiter.py \
  --firmware /path/to/your/firmware.zip \
  --candidates emba \
  --stages unpack \
  --timeout 72000 \
  --experiment-id my-comparison
```

`--timeout 72000` = 20 hours, comfortable headroom over EMBA's observed
13–20 hour real range. Because this uses the **same `--experiment-id`** as
your earlier FirmAE/FIRMADYNE run, all three results are recorded together
under one experiment for a unified report later.

Since this will run for many hours, consider launching it so it survives
you closing your terminal:

```bash
nohup python3 run_firmarbiter.py \
  --firmware /path/to/your/firmware.zip \
  --candidates emba \
  --stages unpack \
  --timeout 72000 \
  --experiment-id my-comparison \
  > /tmp/emba_run.log 2>&1 &
disown
```

Check on it later with:

```bash
tail -20 /tmp/emba_run.log
```

### 8.3 Tuning memory and CPU for your machine

Two flags let you adjust resource limits to fit your actual hardware:

```bash
python3 run_firmarbiter.py \
  --firmware /path/to/your/firmware.zip \
  --candidates emba \
  --stages unpack \
  --timeout 72000 \
  --memory-gb 6 \
  --cpu-cores 6 \
  --experiment-id my-comparison
```

**Important, real guidance on `--memory-gb`:** this is a *hard cap* on the
candidate's own container — it must be set meaningfully **below** your
total system RAM, leaving real headroom for your host OS and anything else
running. Setting this equal to or above your total RAM provides essentially
no real protection and has caused real crashes during this project's own
development. As a starting point, don't set this higher than roughly 60% of
your total system RAM.

### 8.4 Turning results into a readable summary

Once your experiment's runs are complete:

```bash
python3 score_aggregator.py --experiment-id my-comparison
```

This produces a real summary table (unpack%, boot%, reachability%, and more,
per candidate) and writes CSV files under `reports/` for further analysis.

---

## 9. Real, known things worth knowing before you rely on this for a large run

These are genuine, previously-encountered issues, documented honestly so
you don't lose time rediscovering them:

- **Disk space during long EMBA runs**: a single EMBA run can genuinely
  produce 20+ GB of output. Check `df -h` before launching a long run, not
  just when something goes wrong.
- **Old Docker build cache and superseded images accumulate fast**,
  especially if you rebuild adapters multiple times during setup. Check
  periodically:
```bash
  docker system df
```
  and reclaim space safely with:
```bash
  docker builder prune -af
  docker image prune -af
```
  (This only removes cache/images not backing a currently running
  container — safe to run at any time.)
- **A stuck or crashed run leaves containers behind.** Check for orphaned
  containers periodically:
```bash
  docker ps -a --format "table {{.Names}}\t{{.Status}}"
```
  and remove any long-`Exited` FirmArbiter containers you don't need:
```bash
  docker container prune -f
```

---

## 10. If something doesn't work

1. **Re-run `python3 run_firmarbiter.py --list-candidates`** — this catches
   most manifest/build problems early with a specific, real error message.
2. **Check the real, independent evidence, not just the summary line** —
   every run's full `final-result.json` and `contract/events/events.jsonl`
   contain the actual, detailed record of what happened.
3. **If a candidate's own build fails**, the error from `docker build` is
   usually specific and actionable (missing dependency, network issue,
   etc.) — read the actual build output rather than assuming the adapter
   code itself is broken.

---

## 11. What's next after this

Once you've run a real comparison and reviewed the results, you're ready to
scale up — a larger firmware corpus, more candidates as they're fixed, and
a genuine multi-firmware evaluation. That's a bigger undertaking with its
own real planning needs (storage policy for accumulating results, timeout
tuning across many firmware images) — treat this document as your
foundation for that, not the final word on it.

---

## 12. Adding a new candidate tool (one that isn't in `adapters/` yet)

**Read this honestly, both the good and the limiting parts, before
deciding how much time to budget.**

### 12.1 The genuinely good news

`--new-adapter` handles the mechanical setup for you — copying the real,
already-correct shared plumbing (`lifecycle/`, `entrypoint.py`, event
schema) and generating a manifest that's genuinely valid against the real
schema from the start, not the project's own stale example file. You
should not need to touch event emission, heartbeats, or shutdown handling
at all — that's real, meaningful risk-reduction already built in.

### 12.2 The honest limitation

What no scaffolding tool can remove is tool-specific integration work:
your new candidate's own real extraction/boot/reporting logic, and
getting that specific tool's own dependencies to build and run correctly
inside Docker. **Budget your time expecting this part to be genuinely
proportional to how complex the target tool itself is to containerize,**
not to how "hard FirmArbiter is."

### 12.3 Start with the compatibility checker, before writing any code

```bash
python3 tools/assess_candidate.py <path-or-git-url-to-the-tool-you-want-to-add>
```

This inspects the target tool's own repository and reports back on real
compatibility concerns *before* any adapter code exists.

### 12.4 Scaffold the adapter

The simple form:

```bash
python3 run_firmarbiter.py --new-adapter <your-new-candidate-name>
```

This creates `adapters/<name>/` with a complete, working skeleton —
`entrypoint.py`, `lifecycle/`, `schemas/`, a starter `Dockerfile`, and an
`adapter.yaml` that's already structurally valid, just with placeholder
values you need to fill in.

**The better form — let it fetch the real values for you:**

```bash
python3 run_firmarbiter.py --new-adapter <your-new-candidate-name> \
  --source-repo https://github.com/example/your-tool.git \
  --base-image ubuntu:22.04
```

This resolves the real, current commit SHA of your target repo and the
real `@sha256` digest of your chosen base image automatically, and writes
them directly into the manifest — removing two genuinely tedious manual
lookups. **This is opt-in by design, not automatic**: you're still the one
choosing the real source repo and base image; the tool just saves you
looking up their exact pinned values by hand. If either fetch fails for
any reason (no network, a blocked/restrictive network — this was
personally hit and confirmed during this project's own testing on a
university network that blocked Docker's DNS resolution outright, bad
URL), you'll see a clear warning and the manifest falls back to the same
clearly-marked placeholder it would have had anyway — nothing crashes,
nothing silently breaks.

**What still needs a real, human decision either way** — `candidate.name`
(the tool's actual display name) is never auto-filled, since it isn't
safely derivable from a repo URL or image tag.

### 12.5 Fill in exactly three functions

You only edit `run_unpack`, `run_emulate`, and `run_endpoint_discovery`
inside your new `entrypoint.py` (or write them in a separate
`pipeline.py` following `pipeline.py.example`'s pattern and import them).
The real, required contract for each:

- **Return a `(stage_outcome, message)` tuple** — `stage_outcome` is one of
  `"completed"`, `"failed"`, or `"not_applicable"` (use `"not_applicable"`
  immediately if your tool doesn't do that stage at all — e.g. a
  static-analysis-only tool has no real `emulate` or `endpoint-discovery`).
- **Do not raise exceptions for a normal candidate failure** (e.g. your
  tool's extraction command genuinely failing on this firmware) — return
  `("failed", "...")` instead. Exceptions are reserved for real
  adapter/contract-level bugs, which `entrypoint.py` already handles
  correctly (it reports them, still performs a clean shutdown sequence,
  then exits non-zero).
- **`run_unpack` must leave the real, extracted filesystem at
  `<artifacts_path>/unpack/rootfs/`** — not a copied archive.
  FirmArbiter's independent verification inspects that exact path
  directly; this specific mistake is called out in the template because
  it's a real one that already cost real time during this project's own
  FIRMADYNE onboarding.
- **`run_endpoint_discovery` reports your tool's own *claims***, not a
  verified result — FirmArbiter's separate neutral network probe does the
  actual independent reachability check. These two are allowed to
  legitimately disagree.

### 12.6 Write the real Dockerfile for your tool

The generated `Dockerfile` already has the shared `lifecycle/` +
`entrypoint.py` + event schema COPY lines correct — leave those alone.
**The part you genuinely have to figure out yourself is tool-specific**;
here's where to actually look, rather than guess:

- **Your target tool's own README/install instructions** — the real,
  authoritative source for what it needs to build and run.
- **An existing adapter in this project as a real, working reference**
  — `adapters/firmae/Dockerfile` or `adapters/firmadyne/Dockerfile` are
  good starting patterns for a tool you `git clone` at a pinned commit
  and build from source; both show the real, working shape (clone at the
  pinned commit, install build dependencies, apply any needed patches).
  `adapters/emba/Dockerfile` is a better reference if your tool instead
  publishes its own official pre-built image you're extending rather than
  building from source.

### 12.7 Validate early and often

```bash
python3 run_firmarbiter.py --list-candidates
```

Run this as soon as you have a filled-in `adapter.yaml`, before writing
more adapter logic — it validates against the real, enforced schema and
tells you specifically what's wrong if something doesn't pass.

### 12.8 A known, real gap worth knowing about before you start

If your new candidate needs to launch its *own* nested containers (the way
`fact_extractor` does, requiring a mounted Docker socket), be aware this
project's current coordinator does not yet support that pattern (§6.4).
Confirm your target tool doesn't need this before investing significant
time in an adapter for it.