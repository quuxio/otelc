# Security policy

## Supported state

There is no released compiler wrapper or instrumentation runtime yet. Security reports concerning repository tooling, workflows, or the proposed architecture are still welcome.

## Reporting

Use [GitHub private vulnerability reporting](https://github.com/quuxio/otelc/security/advisories/new). Include the affected commit, a minimal reproduction, expected impact, and any relevant environment details. Remove credentials and unrelated private information before submitting. Do not disclose an unpatched vulnerability in a public issue.

## Design requirements

The runtime must bound memory and cardinality, keep arguments/return values out of telemetry, redact exporter credentials, verify manifests against image identities, and never unwind through native probes. Runtime-control access will be restricted to the process owner. See [runtime](docs/runtime.md), [ABI](docs/abi.md), and [configuration](docs/configuration.md) for the detailed contracts.

## Dependabot alerts and security updates

Weekly version-update proposals are configured in `.github/dependabot.yml`. Dependabot alerts and security updates are separate repository settings: the YAML schedule alone does not enable them. Alerts and security updates were enabled and verified as quuxio on 7 October 2026; recheck the live state before relying on this dated observation.

```sh
make github-security-check
make github-security-enable
```

The first command is read-only and fails if alerts or security updates are disabled/paused, permissions are insufficient, or an API response cannot be verified. The second enables disabled settings and then verifies them; it does not dismiss alerts or merge update PRs. It is idempotent when both settings are already enabled. Paused security updates require inspection in GitHub settings.

Both commands obtain only the saved `quuxio` GitHub CLI credential and verify `/user` before repository operations. Inherited tokens, debugging settings and unrelated credentials are excluded. The Mac uses `/opt/homebrew/bin/gh` directly to avoid the local wrapper. Repository administration access is required. Failures must not be interpreted as zero alerts. No administrator token is added to CI.

The JSON result contains verified setting states and a count of open alerts by severity. Counts use the API's cursor links, reject duplicate alerts/repeated cursors and fail rather than report a partial result. They are a current API snapshot, not proof that GitHub's asynchronous dependency analysis is complete or that no unknown vulnerability exists. See [GitHub's security-setting API](https://docs.github.com/en/rest/repos/repos#enable-vulnerability-alerts) and [Dependabot alert pagination](https://docs.github.com/en/rest/dependabot/alerts#list-dependabot-alerts-for-a-repository).
