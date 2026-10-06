# Agent Rules

- If `~/.agents/AGENTS.md` exists, read it and follow it.
- Keep implemented behaviour distinct from proposed behaviour. This repository contains a local native timing prototype and quality tooling. Consult docs/local-implementation.md for validated behaviour and remaining qualification.
- All target-language instrumentation must work without edits to application or dependency source. Use external configuration and build/launch adapters; a language-aware pre-parser may inject code/annotations into generated copies or in-memory input. Existing source annotations may guide selection, but users must not be required to add them. Required edits to source annotations, decorators, imports, guard members or manual probes do not meet this requirement. The current C++ lifetime guard is an interim opt-in prototype, not automatic lifetime support. See docs/design.md#source-free-instrumentation-contract.
- Every language adapter must consume the common schema-2 policy or its resolved JSON. Preserve shared defaults, selection/exclusion rules, limits, OTLP precedence and unsupported-feature errors; keep backend settings under adapters.<language>. See docs/common-configuration.md.
- Use `quuxio/otelc` as the GitHub home and `quuxio_otelc` as the SonarQube project key.
- All work on this project must use the `quuxio` account and its credentials. Never use `stephenlclarke` credentials for this project. Verify the authenticated GitHub login before remote operations.
- Run `make check` for documentation and quality-tooling changes. Run the additional gates in `docs/roadmap.md` when product implementation begins.

For authenticated GitHub operations, use `/opt/homebrew/bin/gh` directly with a verified quuxio token; the local `~/bin/gh` wrapper can override explicit credentials. Stop if the API identity is not `quuxio`.
