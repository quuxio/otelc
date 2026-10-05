# Security policy

## Supported state

There is no released compiler wrapper or instrumentation runtime yet. Security reports concerning repository tooling, workflows, or the proposed architecture are still welcome.

## Reporting

Use [GitHub private vulnerability reporting](https://github.com/quuxio/otelc/security/advisories/new). Include the affected commit, a minimal reproduction, expected impact, and any relevant environment details. Remove credentials and unrelated private information before submitting. Do not disclose an unpatched vulnerability in a public issue.

## Design requirements

The runtime must bound memory and cardinality, keep arguments/return values out of telemetry, redact exporter credentials, verify manifests against image identities, and never unwind through native probes. Runtime-control access will be restricted to the process owner. See [runtime](docs/runtime.md), [ABI](docs/abi.md), and [configuration](docs/configuration.md) for the detailed contracts.
