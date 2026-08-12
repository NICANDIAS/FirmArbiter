# FIRMARBITER Adapter Contract v1

Status: Draft  
Contract version: 1.0  
Benchmark: FIRMARBITER

## 1. Purpose

The FIRMARBITER Adapter Contract defines the interface between the
candidate-neutral FIRMARBITER benchmark core and candidate-specific firmware
analysis adapters.

The contract allows heterogeneous candidates to be installed, launched,
observed, stopped and audited without placing candidate-specific knowledge
inside the FIRMARBITER core.

## 2. Fundamental separation

The FIRMARBITER core owns generic orchestration, experiment-policy enforcement,
independent measurement and result assembly.

The adapter owns candidate installation, native command translation,
candidate-output interpretation, candidate-local lifecycle management and
candidate-local cleanup.

Candidate-reported events are claims. They do not directly establish
independently verified benchmark metrics.

## 3. Contract channels

The adapter receives:

- an immutable firmware object;
- a machine-readable run request;
- a writable private workspace;
- a writable event channel;
- a writable artifact directory.

The adapter produces:

- append-only structured lifecycle events;
- candidate-generated artifacts;
- raw stdout and stderr evidence;
- candidate-local provenance.

The FIRMARBITER core must not parse arbitrary candidate console text to determine
lifecycle state.

## 4. Lifecycle requirement

After reporting boot or an endpoint, the adapter must remain alive until
FIRMARBITER requests shutdown, the candidate exits unexpectedly, or the run is
terminated according to the experiment timeout policy.

## 5. Claim and measurement separation

Candidate claims include candidate-reported extraction, boot and endpoints.

Independent FIRMARBITER measurements include verified unpacking, boot evidence,
service reachability, service authenticity, service stability, compute cost
and environmental residue.

## 6. Result-state semantics

Metrics must distinguish:

- true;
- false;
- not attempted;
- not applicable;
- probe error.

Setup failures must be separated from candidate-analysis failures.
Timeout must be separated from early exit and forced termination.

## 7. Next sections

- Adapter manifest schema
- Run request schema
- Event schema
- Lifecycle state machine
- Endpoint reporting
- Error taxonomy
- Shutdown and cleanup
- Runtime permissions
- Provenance
- Security boundaries
- Contract compatibility
