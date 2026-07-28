# FIRMARBITER

FIRMARBITER — Validated Emulation and Rehosting Integrity Testing and Assessment Suite — is a Python benchmark harness for firmware security analysis tools.

FIRMARBITER does **not** analyse firmware itself. It runs candidate tools such as FirmAE, FIRMADYNE, and EMBA, monitors their execution, and records structured results for fair comparison.

## Current prototype status

This is an early private research prototype.

Current capabilities include:

- firmware discovery from a file or folder
- candidate adapter model
- Docker-based candidate execution
- FirmAE candidate support
- structured JSON result output
- comparison reports through `score_aggregator.py`

## Metrics

FIRMARBITER captures six benchmark metrics:

1. unpack success
2. boot success
3. service reachability
4. service authenticity
5. service stability
6. compute cost

## Repository contents

This repository contains source code, candidate adapters, Dockerfiles, schemas, and setup files.

It does **not** include:

- firmware images
- benchmark result JSON files
- reports
- logs
- Docker images
- virtual machines
- third-party candidate tool repositories

Generated outputs are ignored by Git.

## Configuration

FIRMARBITER uses a local configuration file called `firmarbiter.conf`.

This file is machine-specific and is not committed to the repository.

After cloning, create it from the example file:

    cp firmarbiter.example.conf firmarbiter.conf

Then edit the paths inside `firmarbiter.conf` to match your machine:

    nano firmarbiter.conf

## Setup

Create the Python environment:

    bash setup.sh

Then verify candidates:

    ./python run_firmarbiter.py --list-candidates

## Running FIRMARBITER

Example single-candidate run:

    ./python run_firmarbiter.py \
      --firmware /path/to/firmware_folder \
      --candidate firmae \
      --timeout 900

Generate the comparison report:

    ./python score_aggregator.py

Results are written to:

    results/runs/

Reports are written to:

    reports/

These output folders are generated locally and are not committed to Git.

## Candidate tools

FIRMARBITER does not redistribute FirmAE, FIRMADYNE, EMBA, or firmware images.

Candidate tools must be installed, cloned, or built separately according to their own licences and setup requirements. FIRMARBITER provides adapters and configuration files for running and measuring them.

## Research note

This prototype is part of a PhD research project on reproducible firmware emulation and security-analysis benchmarking. The goal is to measure candidate tools under a consistent external evaluation protocol rather than relying only on each tool's self-reported success.
