# FIRMARBITER Adapter Manifest v1

Status: Draft  
Manifest schema version: 1.0  
Adapter Contract version: 1.0

## 1. Purpose

The adapter manifest is a declarative description of a FIRMARBITER candidate
adapter package.

It allows the FIRMARBITER core to discover, validate, build and authorise an
adapter without containing knowledge of any named candidate.

The manifest is not an execution script and must not contain candidate success
logic, benchmark result logic or arbitrary host commands.

## 2. Discovery

An adapter package contains an `adapter.yaml` file.

The future core will discover adapter packages by scanning the configured
adapter directory. Candidate identifiers are treated as data. The core must
not compare them against hardcoded candidate names.

## 3. Version fields

`schema_version` identifies the structure of the manifest document.

`adapter.version` identifies the version of the adapter implementation.

`adapter.contract_version` identifies the runtime Adapter Contract implemented
by the adapter.

`candidate.source.commit` identifies the exact candidate source revision
included in the adapter image.

These version values are independent and must not be combined.

## 4. Candidate source

Version 1 requires a Git source repository and an exact 40-character commit
hash.

A tag or branch may be recorded as `requested_ref` for human readability, but
it is not authoritative. The pinned commit is authoritative.

Compatibility patches must be stored as files inside the adapter package.
Each patch entry records its relative path, SHA-256 hash and purpose.

## 5. Build declaration

The manifest declares:

- Docker build context;
- Dockerfile path;
- target image platform;
- all digest-pinned base images used by the Dockerfile.

Mutable base-image tags without a digest are not sufficient for a
reproducible adapter build.

The final candidate image digest is not declared in advance. It is recorded by
the provenance recorder after the image has been built.

## 6. Runtime requirements

Runtime requirements are expressed using controlled permission tokens.

The manifest must not contain raw Docker arguments.

The neutral runtime backend maps approved permission tokens to container
runtime settings. Experiment policy may reject an adapter whose requested
permissions are not permitted.

A permission declaration records a requirement. It does not automatically
authorise that requirement.

## 7. Capabilities

Capabilities describe what the candidate can attempt. They do not represent
successful benchmark outcomes.

For example, declaring the `emulate` stage does not establish boot success.

Architecture and vendor hints are declared as:

- `unsupported`;
- `optional`;
- `required`.

Any supplied hint must come from the experiment manifest or independently
recorded corpus metadata. An adapter must not receive undisclosed
candidate-specific assistance.

## 8. Input invariant

All adapters receive the same canonical firmware bytes through the standard
read-only contract input.

An adapter may create a private working copy and perform candidate-native
preprocessing inside its workspace.

Such transformations do not modify the canonical input and must be recorded as
adapter provenance.

## 9. Forbidden content

The following information is forbidden in an adapter manifest:

- candidate success phrases or parsing expressions;
- fixed target IP addresses or service ports;
- metric values, score weights or pass thresholds;
- per-candidate experiment timeouts or resource budgets;
- retry policies;
- raw container runtime arguments;
- arbitrary host commands;
- host cleanup commands;
- secrets or credentials;
- firmware replacement or mutation instructions;
- candidate-reported success represented as verified success;
- custom event-channel paths or event transports.

Unknown properties are rejected by the schema.
