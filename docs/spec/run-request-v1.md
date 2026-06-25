# VERITAS Run Request v1

Status: Draft  
Run-request schema version: 1.0  
Adapter Contract version: 1.0

## 1. Purpose

A VERITAS run request is the machine-readable instruction supplied to one
adapter for one firmware execution attempt.

A distinct request is created for every firmware, adapter and repetition.

The request contains experiment-controlled input identity, lifecycle limits,
approved runtime grants, requested candidate stages, optional disclosed hints
and standard contract paths.

It must not contain expected benchmark outcomes or information obtained from
previous candidate executions.

## 2. Standard location

The request is mounted read-only at:

`/veritas/input/request.json`

The canonical firmware object is mounted read-only at:

`/veritas/input/firmware`

The complete `/veritas/input` directory is read-only.

## 3. Firmware input semantics

Version 1 defines the firmware delivery semantic as:

`opaque-original-bytes`

This means the adapter receives the exact bytes selected by the experiment
manifest.

VERITAS must not automatically unzip, unpack, rename based on candidate
identity, select an archive member or otherwise transform the canonical
firmware object before delivery.

The canonical firmware filename is always `firmware`. The original corpus
filename is not supplied to the adapter because filenames may expose vendor,
model, architecture or packaging hints.

An adapter may create a private copy inside `/veritas/work` and perform its
native preprocessing there. The adapter must not modify the canonical input.

## 4. Run identity

Every request identifies:

- the experiment;
- the run;
- the adapter;
- the repetition number;
- the requested candidate stages;
- the request creation time.

The adapter identifier must match the validated adapter manifest.

The requested stages must be a subset of the capabilities declared by that
manifest.

## 5. Lifecycle policy

The request defines:

- maximum execution time;
- required heartbeat interval;
- heartbeat-loss threshold;
- graceful-shutdown allowance.

The maximum execution time covers candidate execution after successful
preflight and container startup.

Image building, adapter installation and contract validation are setup
activities and must be measured separately.

Heartbeat loss is evidence of adapter unresponsiveness. It is not, by itself,
proof that the emulated firmware failed to boot.

## 6. Resource budget

The request records the resource limits applied to the candidate execution.

Resource limits are experiment policy and must not vary by adapter unless the
experiment explicitly defines, discloses and records a different comparison
class.

The adapter cannot increase these limits.

## 7. Runtime grants

The adapter manifest declares required runtime privileges.

The run request records the exact grants approved by experiment policy.

The following rules apply:

1. grants must not exceed the manifest declaration;
2. grants must be permitted by experiment policy;
3. requirements must not be silently added by the runtime backend;
4. required permissions must not be silently removed;
5. an unsatisfied requirement produces `configuration_incompatible`;
6. `configuration_incompatible` is not a candidate-analysis failure.

Raw Docker arguments are forbidden.

## 8. Hints

Architecture and vendor hints are optional request fields.

A hint may be supplied only when:

- the adapter manifest declares it optional or required;
- the experiment hint policy permits it;
- its source is recorded;
- equivalent metadata is made available under the same policy to every
  candidate in the comparison.

If an adapter declares a hint as required and the experiment cannot provide
it, the run must not start. The outcome is `configuration_incompatible`.

An omitted hint is different from an explicit `unknown` value.

Hints must not be derived from another candidate's results.

## 9. Standard writable locations

The adapter receives these standard locations:

- `/veritas/work` for private temporary candidate state;
- `/veritas/artifacts` for retained candidate artifacts;
- `/veritas/events/events.jsonl` for append-only contract events;
- `/veritas/control` for lifecycle-control messages.

Paths are fixed by Contract v1 and cannot be replaced by adapter-defined
paths.

## 10. Integrity bindings

The request records the SHA-256 hashes of:

- the canonical firmware object;
- the validated adapter manifest;
- the machine-readable experiment manifest.

The neutral provenance recorder independently records the request file hash,
candidate image digest and runtime configuration.

An adapter-provided hash is never treated as authoritative provenance.

## 11. Cross-document validation

The following checks cannot be expressed fully by JSON Schema and must be
performed by neutral contract validation:

- `adapter_id` matches the discovered adapter manifest;
- requested stages are supported by the adapter;
- runtime grants match approved manifest requirements;
- supplied hints comply with manifest requirements;
- the firmware size and SHA-256 match the mounted object;
- the firmware and request mounts are read-only;
- experiment and adapter-manifest hashes match the validated documents;
- lifecycle limits comply with the experiment manifest;
- resource limits comply with experiment policy.

Validation occurs before candidate execution.

## 12. Forbidden fields and content

A run request must not contain:

- candidate-native commands;
- candidate source installation instructions;
- patches;
- success phrases or parsing expressions;
- expected boot outcomes;
- expected endpoint addresses or ports;
- expected page content;
- benchmark scores or pass thresholds;
- previous candidate results;
- positive-control or negative-control labels;
- per-candidate retries;
- arbitrary host paths;
- raw Docker arguments;
- host cleanup commands;
- secrets or credentials;
- instructions to mutate or replace the canonical firmware input;
- candidate-reported claims represented as verified measurements.

Unknown properties are rejected by the schema.
