PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
MARKDOWNLINT ?= $(if $(wildcard ci/markdownlint/node_modules/.bin/markdownlint),ci/markdownlint/node_modules/.bin/markdownlint,markdownlint)
SONAR_SCANNER ?= sonar-scanner

.PHONY: help setup lint test check build examples benchmark benchmark-live developer-examples stack-up stack-down rust-check model-check rust-coverage sonar-policy scan

help:
	@printf '%s\n' 'setup         Install local validation dependencies' 'lint          Lint all Markdown' 'test          Run offline tests and enforce 90% coverage' 'check         Run all local quality and coverage gates' 'sonar-policy  Verify the remote Previous version policy' 'scan          Analyze a clean commit and wait for its quality gate' 'build         Build the local Rust CLI and native runtime' 'examples      Build the instrumented C and C++ example apps' 'benchmark     Paired plain/probes-disabled/metrics benchmark' 'benchmark-live Same-process metrics-off/on latency comparison' 'developer-examples Build unchanged/annotated/live developer examples' 'stack-up      Start the local Docker metrics stack' 'stack-down    Stop/remove stack containers, keep data' 'rust-check    Rust format, Clippy, unit and native integration tests' 'model-check   Model-check runtime queue publication with Loom' 'rust-coverage Enforce 80% Rust and native-runtime line coverage'

setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.txt
	npm ci --ignore-scripts --prefix ci/markdownlint

lint:
	$(MARKDOWNLINT) '**/*.md' --ignore '**/node_modules/**' --ignore '.venv/**' --ignore '.scannerwork/**'

test:
	mkdir -p build
	$(PYTHON) -m coverage run --source=ci,scripts -m unittest discover -s tests -v
	$(PYTHON) -m coverage report --fail-under=90
	$(PYTHON) -m coverage xml -o build/coverage.xml

check: lint test rust-check model-check rust-coverage

sonar-policy:
	$(PYTHON) ci/verify_sonar_policy.py

scan: check sonar-policy
	@test -z "$$(git status --porcelain)" || { printf '%s\n' 'Commit changes before an authoritative scan.' >&2; exit 1; }
	$(SONAR_SCANNER) -Dsonar.projectVersion="$$(git rev-parse HEAD)" -Dsonar.qualitygate.wait=true

build:
	$(PYTHON) ci/build_llvm_plugin.py
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

rust-check: build
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
