# legacy/

Everything under here predates the Adapter Contract v1 architecture
(`firmarbiter_core/`, `adapters/`, `schemas/`) and is kept for
research/evidence provenance only — some architecture and evidence docs
elsewhere in this repo reference these files' history.

**Do not use anything here as a starting point for new work.**

- `docker_runner.py`, `probes/` — the pre-contract runner and probe
  scripts. Superseded by `firmarbiter_core/docker_backend.py` and
  `firmarbiter_core/probes/` (note: two different directories were both
  called `probes/` before this move — the live one is
  `firmarbiter_core/probes/`).
- `firmarbiter_register.py` — the pre-contract `--register-tool`
  candidate-fingerprinting flow. Nothing in the current
  `run_firmarbiter.py` calls this; the current equivalent is
  `--new-adapter` (scaffold) + `--list-candidates` (discover/validate).
- `candidates/` — the pre-contract per-tool folders
  (`candidate.conf`, `run_adapter.sh`, etc.) that `firmarbiter_register.py`
  and `docker_runner.py` operated on. Superseded by `adapters/`.

For current adapter development, start at `adapters/_template/` and
look at `adapters/fact_extractor/` or `adapters/emba/` as worked
examples — not `adapters/firmae/` or `adapters/firmadyne/`, which
predate the shared `lifecycle/` code (see the header comment in each
of those two files).
