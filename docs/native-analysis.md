# Native compiler and SDK analysis

<!-- markdownlint-disable MD013 -->

The native pipeline covers `native/llvm/OtelcPass.cpp`, `adapters/native/src/metric_control.cpp`, `adapters/native/src/function_runtime.cpp` `adapters/native/launcher/src/runtime.cpp` and `adapters/native/gate/ExecGate.c`. Sonar analysis includes their headers, both SDK test translation units and the ordinary C/C++ compiler fixtures. The launcher gate also executes unchanged applications against real SDK HTTP exporters. This component analysis does not establish broader language admission, production downstream receipt, task/service propagation or lifetime support.

## Actual build context

The LLVM builder retains `compile_commands.json` with the actual compiler argv, source and fixed working directory, bound into its existing plugin evidence. The SDK checks ask CMake for their actual compilation databases. The analysis collector preserves exactly nine compiled translation units: the pass, controls, function engine, launcher runtime, pre-exec gate, both SDK tests and both ordinary compiler fixtures. Its pass context comes from the explicit source-aware variant, covering base, registration and source-selection code; the variants retain separate coverage floors and artifacts. The launcher contributes only its actual runtime entry, avoiding duplicate control/engine entries from its SDK build. The collector refuses missing or duplicate units and missing compilation directories. It does not execute recorded commands or invent include paths and definitions.

The Sonar CI job sets `OTELC_CPP_ANALYSIS=1` through both compilation and scanning. This keeps the fresh SDK dependency headers and build directories present at the paths used by the compiler. Other check jobs use normal temporary-directory cleanup. An analysis run retains this additional scratch only for that runner's lifetime; a manually selected local analysis run needs the same context until its scan finishes.

CI pins both `CC` and `CXX` to that LLVM installation. Native Python compilation also honours `OTELC_LLVM_BIN` when no explicit `CC` is supplied, so the compiler and coverage merger do not silently come from different installations. An explicit operator `CC` still takes precedence.

## Exact coverage

Actual `llvm-cov export -format=lcov` reports retain every covered and uncovered executable line. The collector checks the five exact production source identities, LCOV and database identities, source names and line totals. It requires both native implementation records before separating the combined SDK report, plus a separate complete launcher runtime record. LLVM's aggregate summary and physical line entries can have different denominators in the same export. The collector checks the aggregate totals against the producer's LLVM JSON summary and uses only the physical `DA` entries for Sonar's line-by-line data. Both the component summary floor and the physical-line report must independently reach 80%. Missing, repeated, foreign, stale or inconsistent records fail rather than becoming an empty or apparently complete report.

`make test` writes `build/native-sonar-coverage.xml` after the real native execution. The generic XML uses repository-relative file paths and does not substitute function or aggregate percentages for line coverage. Its Python converter has a separate 80% component floor and focused refusal tests. The normal Python, Java, Node and Go reports remain separate.

For a focused local analysis-context check with the installed matched LLVM 22 tools:

```sh
OTELC_CPP_ANALYSIS=1 make native-check native-launch-check llvm-check PYTHON=.venv/bin/python
PYTHONPATH=tests .venv/bin/python -m unittest test_native_gate
OTELC_CPP_ANALYSIS=1 .venv/bin/python -m ci.native_sonar
```

The final compilation database is `build/native-compile-commands.json`. The hosted scanner uses `sonar.cfamily.compile-commands` and imports `build/native-sonar-coverage.xml` through `sonar.coverageReportPaths`, following [Sonar's compilation-database example](https://github.com/sonarsource-cfamily-examples/linux-cmake-compdb-gh-actions-sc) and [generic coverage format](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/test-coverage/generic-test-data). A local report or a configured property is not proof that hosted C++ analysis passed; review the final commit's actual scanner and quality-gate results before merge.
