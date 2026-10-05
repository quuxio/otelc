# Contributing

otelc is currently at the design stage. Start with the [architecture](docs/design.md), [support boundaries](docs/support.md), and [roadmap](docs/roadmap.md). Keep proposals honest about implemented behaviour and backed by evidence for compiler, runtime, and telemetry claims.

## Changes

- Open a focused issue for a substantial change and describe the user-visible contract it will deliver.
- Use Conventional Commits for commits and pull request titles. Sign development commits when a verified signing key is available for the contributor's GitHub account.
- Keep README, design documents, examples, and behaviour consistent.
- Add deterministic tests for material implementation changes. Aim for at least 90% line coverage with meaningful tests.
- Avoid secrets, raw private data, and absolute developer-machine paths in committed evidence.

For this design/tooling checkout, run `make setup` and `make check`. The [quality guide](docs/quality.md) explains the authenticated SonarQube checks. Future native implementation must also satisfy its [release gates](docs/roadmap.md).

Contributions are accepted under the repository's [AGPL-3.0 license](LICENSE). No contribution automatically adds a runtime linking exception or grants trademark rights.
