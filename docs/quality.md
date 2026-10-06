# Repository quality and SonarQube

## Current scope

This checkout contains a native instrumentation prototype, native fixtures, architecture documents, GitHub workflows and Python validation helpers. Python CI helpers and the benchmark runner are repository tooling; product code lives in the Cargo workspace, `native/llvm/`, `include/otelc/` and `adapters/`.

The SonarQube analysis covers Python CI helpers, their tests, and GitHub workflows. Rust and native product coverage is generated separately and enforced by the 80% product quality gate; it is not included in the published SonarQube badges. Markdown is checked by markdownlint. The badge metrics must be interpreted within this scope.

## Local workflow

Requirements: Rust 1.98+, LLVM 22/Clang, Python 3.12+, Node.js 24.11+, Make, and Git. `sonar-scanner` is needed only for local scanning. `make setup` creates a local virtual environment and installs coverage from hash-verified binary wheels, then installs `markdownlint-cli` 0.49.1 and its dependencies from the committed npm lockfile with package scripts disabled.

The npm overrides pin patched `js-yaml`, KaTeX, and `smol-toml` releases while retaining the current Markdown CLI. Check for dependency advisories with `npm audit --prefix ci/markdownlint` when updating the lockfile.

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
| `test` | Run deterministic tooling/benchmark tests with 90% coverage and produce XML |
| `check` | Run Markdown lint, tooling tests/coverage and native/Python/Node product quality gates |
| `node-check` | Validate in-memory JavaScript instrumentation and enforce 80% adapter coverage |
| `python-check` | Validate unchanged Python instrumentation and enforce 80% adapter coverage |
| `benchmark-language` | Same-process metrics-off/on comparison for `LANGUAGE=python` or `javascript` |
| `build` | Build the LLVM pass, Rust CLI and native runtime archive |
| `examples` | Build callback, exception and lifetime apps in `build/native/` |
| `developer-examples` | Build the configuration-only, annotated and live-control tutorial apps |
| `benchmark` | Compare plain, disabled-probe and active timing builds |
| `benchmark-live` | Compare plain execution with live metrics off/on in one instrumented process |
| `stack-up` / `stack-down` | Start/remove local metrics stack containers, retaining data |
| `rust-check` | Rust formatting, Clippy, unit and native integration tests |
| `model-check` | Loom queue publication/reuse model |
| `rust-coverage` | Enforce at least 80% product line coverage, including the native shim, LLVM pass and lifetime header |
| `sonar-policy` | Verify project-level `sonar.leak.period=previous_version` remotely |
| `scan` | Check a clean Git checkout, verify policy, and scan the exact commit with quality-gate wait |

Rust coverage requires cargo-llvm-cov and Clang/llvm-cov/llvm-profdata matching rustc. Set LLVM_COV and LLVM_PROFDATA explicitly for Homebrew LLVM if cargo cannot find bundled tools. The coverage workflow builds the instrumented static runtime before executing the native fixtures. It generates `build/rust-coverage.lcov` and fails below 80% line coverage. Test sources, build scripts, vendor code and standard-library sources are excluded; Rust product code, the C shim, the LLVM pass and the C++ lifetime header are measured. Native coverage maps are exported individually to avoid collisions between C ABI function names in Rust test binaries and the linked runtime. Source paths and line hits are then merged into one line report. Cached workspace maps and profiles are cleared before each run; a supported release still requires the roadmap's 90% product coverage gate and authenticated analysis of its exact commit.

## SonarQube Cloud project

- Project key: `quuxio_otelc`.
- Dashboard: [SonarQube Cloud](https://sonarcloud.io/summary/new_code?id=quuxio_otelc).
- Organisation: `quuxio`.
- GitHub repository: [quuxio/otelc](https://github.com/quuxio/otelc).
- New-code definition: project-level **Previous version**.
- Authoritative analysis version: the exact lowercase 40-character Git commit SHA.

The helper reads the remote project setting before every authenticated scan and fails if it is absent, differs, or is marked as inherited. A rolling-day window, mutable branch label, or inherited setting without an explicit project value does not satisfy the policy. CI analysis replaces automatic analysis so versioning and imported coverage have one authoritative source.

Store `SONAR_TOKEN` as a GitHub Actions secret. Do not put credentials into `sonar-project.properties`, URLs, logs, or documentation. The operator's local quux credential is `~/.secrets/SONAR_TOKEN_QUUXIO`; load it into `SONAR_TOKEN` for command-line operations. Repository checks work without live SonarQube access; only the policy and scan targets require it.

## CI

The workflow lints documents, runs Python helper tests and coverage, validates native fixtures and enforces 80% product coverage on macOS ARM64, and runs SonarQube on trusted push/manual/same-repository PR events. Fork and Dependabot PRs get local-quality checks without receiving a SonarQube token; their merged changes are analysed on `main` or `master`. Both branches trigger push and pull-request checks. The SonarQube job checks out the actual PR head commit and supplies that exact SHA as `sonar.projectVersion`.

The helper is analyzed as Python and imports `build/coverage.xml`. Tests use fake HTTP responses; they never rely on live infrastructure or consume a credential. SonarQube's current quality gate is checked after upload. A missing CI credential is a failure for an authenticated scan, not a successful skipped analysis. Actions are pinned to full commit hashes; Python wheels and npm packages are locked and installed without running dependency build or lifecycle scripts.

Manually provisioned projects need separate GitHub ALM binding if automatic PR decoration is desired. Main-branch scanning and badge metrics do not depend on claiming that such a binding already exists.

## Badges and traffic

The README keeps the full SonarQube badge set from `fixdecoder_rs`: quality gate, bugs, code smells, coverage, duplication, lines of code, reliability, security, debt, maintainability, and vulnerabilities. It also links CI, the design-stage status, the license, and repository traffic.

The traffic workflow queries GitHub's views endpoint using a repository secret `TRAFFIC_TOKEN`, then updates `.badges/traffic.json` for Shields. Missing credentials or an unsuccessful API response fail explicitly. Scheduled updates are maintenance only; `.badges` changes do not rerun the design CI workflow. The placeholder badge says it is awaiting an update rather than claiming zero views.

The repository uses Conventional Commits and recommends signing development commits with a verified key for the contributor's GitHub account. Automated traffic updates use GitHub's bot identity.
