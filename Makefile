PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
MARKDOWNLINT ?= $(if $(wildcard ci/markdownlint/node_modules/.bin/markdownlint),ci/markdownlint/node_modules/.bin/markdownlint,markdownlint)
SONAR_SCANNER ?= sonar-scanner
STATICCHECK ?= $(CURDIR)/build/go-tools/staticcheck
GOVULNCHECK ?= $(CURDIR)/build/go-tools/govulncheck

.PHONY: help setup lint test check build examples benchmark benchmark-live benchmark-language trace-check python-check node-build node-check java-build java-check go-build go-tools go-check developer-examples stack-up stack-down rust-check model-check rust-coverage sonar-policy github-security-check github-security-enable scan

help:
	@printf '%s\n' 'setup         Install local validation dependencies' 'lint          Lint all Markdown' 'test          Run offline tests and enforce 90% coverage' 'check         Run all local quality and coverage gates' 'sonar-policy  Verify the remote Previous version policy' 'github-security-check Verify live Dependabot settings and alerts as quuxio' 'github-security-enable Enable and verify Dependabot settings as quuxio' 'scan          Analyze a clean commit and wait for its quality gate' 'build         Build the local Rust CLI and native runtime' 'examples      Build the instrumented C and C++ example apps' 'benchmark     Paired plain/probes-disabled/metrics benchmark' 'benchmark-live Same-process metrics-off/on latency comparison' 'developer-examples Build unchanged/annotated/live developer examples' 'python-check  Validate Python adapter with 80% product coverage' 'node-build    Build the Promise observer for the running Node version' 'node-check    Validate Node adapter with 80% product coverage' 'java-check    Build Java agent and enforce 80% product coverage' 'go-tools      Build locked Go analysers' 'go-check      Validate Go adapter, vulnerabilities and 80% coverage' 'benchmark-language Live metrics comparison for LANGUAGE=python|javascript|typescript|java|go|rust' 'trace-check   Verify all unchanged language examples in local Tempo' 'stack-up      Start the local Docker metrics stack' 'stack-down    Stop/remove stack containers, keep data' 'rust-check    Rust format, Clippy, unit and native integration tests' 'model-check   Model-check runtime queue publication with Loom' 'rust-coverage Enforce 80% Rust and native-runtime line coverage'

setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.txt
	.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r adapters/python/requirements.txt
	npm ci --ignore-scripts --prefix ci/markdownlint
	npm ci --ignore-scripts --prefix adapters/node

lint:
	$(MARKDOWNLINT) '**/*.md' --ignore '**/node_modules/**' --ignore '.venv/**' --ignore '.scannerwork/**'

test:
	mkdir -p build
	$(PYTHON) -m coverage run --source=ci,scripts -m unittest discover -s tests -v
	$(PYTHON) -m coverage report --fail-under=90
	$(PYTHON) -m coverage xml -o build/coverage.xml

check: build java-build go-build lint test rust-check model-check rust-coverage python-check node-check java-check go-check

trace-check: build java-build go-build
	$(PYTHON) scripts/trace_check.py $(TRACE_CHECK_ARGS)

github-security-check:
	$(PYTHON) ci/github_security.py

github-security-enable:
	$(PYTHON) ci/github_security.py --enable

sonar-policy:
	$(PYTHON) ci/verify_sonar_policy.py

scan: check sonar-policy
	@test -z "$$(git status --porcelain)" || { printf '%s\n' 'Commit changes before an authoritative scan.' >&2; exit 1; }
	$(SONAR_SCANNER) -Dsonar.projectVersion="$$(git rev-parse HEAD)" -Dsonar.qualitygate.wait=true

build:
	$(PYTHON) ci/build_llvm_plugin.py
	$(PYTHON) ci/build_node_observer.py
	cargo build --workspace --locked

examples: build
	mkdir -p build/native
	./target/debug/quux-otelc --config examples/local.toml clang -O2 -g tests/fixtures/timing.c -o build/native/timing
	./target/debug/quux-otelc --config examples/local.toml clang++ -O2 -g -fno-exceptions tests/fixtures/timing.cpp -o build/native/timing-cpp

	./target/debug/quux-otelc --config examples/exceptions.toml clang++ -O2 -g -std=c++20 tests/fixtures/exceptions.cpp -o build/native/exceptions
	./target/debug/quux-otelc --config examples/exceptions.toml clang++ -O2 -g -std=c++20 -Iinclude tests/fixtures/objects.cpp -o build/native/objects

benchmark: build
	$(PYTHON) scripts/benchmark.py $(BENCHMARK_ARGS)

stack-up:
	docker compose up -d

stack-down:
	docker compose down

rust-check: build java-build go-build
	cargo fmt --all -- --check
	cargo clippy --workspace --all-targets --locked -- -D warnings
	cargo test --workspace --locked -- --test-threads=1

model-check:
	cargo test -p quux-otelc-runtime --features loom-tests --locked --lib queue::model

rust-coverage:
	mkdir -p build
	$(PYTHON) ci/rust_coverage.py

developer-examples: build
	mkdir -p build/tutorial
	./target/debug/quux-otelc --config examples/config-only.toml clang -O2 -g examples/apps/config-only.c -o build/tutorial/config-only
	./target/debug/quux-otelc --config examples/annotated.toml clang -O2 -g examples/apps/annotated.c -o build/tutorial/annotated-c
	./target/debug/quux-otelc --config examples/annotated.toml clang++ -O2 -g -std=c++20 examples/apps/annotated.cpp -o build/tutorial/annotated-cpp
	./target/debug/quux-otelc --config examples/live.toml clang++ -O2 -g -std=c++20 examples/apps/live-latency.cpp -o build/tutorial/live

benchmark-live: build
	$(PYTHON) scripts/benchmark_live.py $(LIVE_BENCHMARK_ARGS)

python-check:
	cargo build -p quux-otelc-cli --locked
	$(PYTHON) -m coverage run --data-file=build/python.coverage --source=adapters/python -m unittest discover -s tests -p 'test_python_*.py' -v
	$(PYTHON) -m coverage report --data-file=build/python.coverage --fail-under=80
	$(PYTHON) -m coverage report --data-file=build/python.coverage --include='*/quux_otelc_python/traces.py' --fail-under=80
	$(PYTHON) -m coverage report --data-file=build/python.coverage --include='*/quux_otelc_python/task_context.py' --fail-under=80
	$(PYTHON) -m coverage report --data-file=build/python.coverage --include='*/quux_otelc_python/worker_context.py' --fail-under=80
	$(PYTHON) -m coverage xml --data-file=build/python.coverage -o build/python-coverage.xml

benchmark-language: build
	$(PYTHON) scripts/benchmark_languages.py --language $(LANGUAGE) $(LANGUAGE_BENCHMARK_ARGS)

node-build:
	$(PYTHON) ci/build_node_observer.py

node-check: node-build
	cargo build -p quux-otelc-cli --locked
	npm test --prefix adapters/node
	cd adapters/node && node --test --experimental-test-coverage --test-coverage-include='**/adapters/node/typescript-native.mjs' --test-coverage-include='**/adapters/node/typescript-identities.mjs' --test-coverage-lines=80 tests/typescript-native.test.mjs
	cd adapters/node && node --test --experimental-test-coverage --test-coverage-include='**/adapters/node/traces.mjs' --test-coverage-lines=80 tests/spans.test.mjs
	cd adapters/node && node --test --experimental-test-coverage --test-coverage-include='**/adapters/node/trace-exporter.mjs' --test-coverage-lines=80 tests/spans.test.mjs

java-build:
	cd adapters/java && mvn -B -DskipTests package

java-check:
	cd adapters/java && mvn -B verify

go-build:
	cd adapters/go && go build ./runtime
	cd adapters/go && go build -o build/otelc-go ./cmd/otelc-go

go-tools:
	mkdir -p build/go-tools
	cd ci/go-tools && go build -mod=readonly -o ../../build/go-tools/staticcheck honnef.co/go/tools/cmd/staticcheck
	cd ci/go-tools && go build -mod=readonly -o ../../build/go-tools/govulncheck golang.org/x/vuln/cmd/govulncheck

go-check: go-build go-tools
	cd adapters/go && test -z "$$(gofmt -l policy runtime adapter cmd)"
	cd adapters/go && go vet ./...
	cd adapters/go && $(STATICCHECK) ./...
	cd adapters/go && $(GOVULNCHECK) ./...
	cd adapters/go && go test -race -coverprofile=build/coverage.out ./...
	cd adapters/go && go tool cover -func=build/coverage.out | awk '/^total:/ { gsub("%", "", $$3); print; found=1; if ($$3 < 80) exit 1 } END { if (!found) exit 1 }'
	cd adapters/go && awk '/\/runtime\/(traces|trace_scope|trace_export)\.go:/ { split($$1, fields, ":"); total[fields[1]] += $$2; if ($$3 > 0) hit[fields[1]] += $$2 } END { for (file in total) { files++; percentage = 100 * hit[file] / total[file]; printf "%s statement coverage: %.2f%% (minimum 80%%)\n", file, percentage; if (percentage < 80) failed=1 } if (files != 3 || failed) exit 1 }' build/coverage.out
