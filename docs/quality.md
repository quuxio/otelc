# Repository quality and SonarQube

## Current scope

This checkout contains architecture documents, an example configuration, GitHub workflows, and a small Python helper that verifies the SonarQube new-code policy. Python is repository tooling; the intended product is Rust plus native compiler integration.

The SonarQube analysis currently measures `ci/verify_sonar_policy.py`, its tests, and the GitHub workflows. It does not measure a compiler wrapper, runtime, or exporter because those do not exist yet. Markdown is checked by markdownlint. The badge metrics must be interpreted within this scope.

## Local workflow

Requirements: Python 3.11+, Node.js 24+, Make, and Git. `sonar-scanner` is needed only for local scanning. `make setup` creates a local virtual environment and installs coverage from hash-verified binary wheels, then installs `markdownlint-cli` 0.49.1 and its dependencies from the committed npm lockfile with package scripts disabled.

```sh
make setup
make check

# With an existing SonarQube credential in the environment:
make sonar-policy
make scan
```

| Target | Behaviour |
| --- | --- |
| `help` | List the supported repository workflows |
| `setup` | Create `.venv` and install validation dependencies |
| `lint` | Lint all repository Markdown |
| `test` | Run deterministic unit tests and produce a coverage XML report |
| `check` | Run Markdown lint and tests with at least 90% helper line coverage |
| `sonar-policy` | Verify project-level `sonar.leak.period=previous_version` remotely |
| `scan` | Check a clean Git checkout, verify policy, and scan the exact commit with quality-gate wait |

No product build/test command is advertised until the first runtime vertical slice exists. At that point add Cargo formatting, Clippy, native fixtures, and Rust coverage targets rather than retain Python-tooling coverage as the product headline.

## SonarQube Cloud project

- Project key: `quuxio_otelc`.
- Dashboard: [SonarQube Cloud](https://sonarcloud.io/summary/new_code?id=quuxio_otelc).
- Organisation: `quuxio`.
- GitHub repository: [quuxio/otelc](https://github.com/quuxio/otelc).
- New-code definition: project-level **Previous version**.
- Authoritative analysis version: the exact lowercase 40-character Git commit SHA.

The helper reads the remote project setting before every authenticated scan and fails if it is absent or differs. A rolling-day window, mutable branch label, or inherited setting without an explicit project value does not satisfy the policy. CI analysis replaces automatic analysis so versioning and imported coverage have one authoritative source.

Store `SONAR_TOKEN` as a GitHub Actions secret. Do not put credentials into `sonar-project.properties`, URLs, logs, or documentation. The operator's local quux credential is `~/.secrets/SONAR_TOKEN_QUUXIO`; load it into `SONAR_TOKEN` for command-line operations. Repository checks work without live SonarQube access; only the policy and scan targets require it.

## CI

The workflow lints documents, runs policy-helper tests and coverage, and runs SonarQube on trusted push/manual/same-repository PR events. Fork and Dependabot PRs get local-quality checks without receiving a SonarQube token; their merged changes are analyzed on `main`. The SonarQube job checks out the actual PR head commit and supplies that exact SHA as `sonar.projectVersion`.

The helper is analyzed as Python and imports `build/coverage.xml`. Tests use fake HTTP responses; they never rely on live infrastructure or consume a credential. SonarQube's current quality gate is checked after upload. A missing CI credential is a failure for an authenticated scan, not a successful skipped analysis. Actions are pinned to full commit hashes; Python wheels and npm packages are locked and installed without running dependency build or lifecycle scripts.

Manually provisioned projects need separate GitHub ALM binding if automatic PR decoration is desired. Main-branch scanning and badge metrics do not depend on claiming that such a binding already exists.

## Badges and traffic

The README keeps the full SonarQube badge set from `fixdecoder_rs`: quality gate, bugs, code smells, coverage, duplication, lines of code, reliability, security, debt, maintainability, and vulnerabilities. It also links CI, the design-stage status, the license, and repository traffic.

The traffic workflow queries GitHub's views endpoint using a repository secret `TRAFFIC_TOKEN`, then updates `.badges/traffic.json` for Shields. Missing credentials or an unsuccessful API response fail explicitly. Scheduled updates are maintenance only; `.badges` changes do not rerun the design CI workflow. The placeholder badge says it is awaiting an update rather than claiming zero views.

The repository uses Conventional Commits and recommends signing development commits with a verified key for the contributor's GitHub account. Automated traffic updates use GitHub's bot identity.
