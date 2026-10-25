PYTHON ?= .venv/bin/python
OTELC_TEST_PYTHON ?= $(PYTHON)
MAVEN ?= mvn
GO ?= go
NODE ?= $(if $(CI),node,$(if $(wildcard /opt/homebrew/opt/node@24/bin/node),/opt/homebrew/opt/node@24/bin/node,node))
NODE_PATH_PREFIX = $(if $(findstring /,$(NODE)),$(dir $(NODE)):)
NPM_CACHE ?= $(CURDIR)/build/npm-cache
BENCHMARK_LANGUAGE ?= java
BENCHMARK_CONFIG ?= examples/otelc.toml
LLVM_OUTPUT ?= build/llvm-plugin
NATIVE_LAUNCH_OUTPUT ?= build/native-launch-sdk

.PHONY: help setup check lint test go-check node-check node-observer node-native-coverage node-http python-inputs python-inputs-check llvm-plugin llvm-check native-check native-launch-sdk native-launch-check java-build java-check java-context-examples java-witness-bindings stack-up stack-down stack-check benchmark sonar-policy
.NOTPARALLEL: check

help:
	@printf '%s\n' 'setup       Install locked Python, Node SDK and Markdown tools' 'check       Run lint, adapter tests and native compiler/SDK component coverage gates' 'go-check    Run SDK gate race tests and require 80% Go statements overall and per file' 'node-check  Check Microsoft compiler types and source/SDK/native coverage' 'node-observer Build the public-V8 observer for the selected Node version/ABI' 'node-http   Generate the source-bound official HTTP preservation product' 'python-inputs Build the candidate native CPython entry/import/code input component' 'python-inputs-check Profile native entry/import/code cases and require 80% per-file lines' 'llvm-plugin Build the LLVM 22 function-hook component in a new directory' 'llvm-check  Check real C/C++/IR compilation and 80% native/Python coverage' 'native-check Check native SDK controls/functions/object probes and 80% coverage' 'native-lifetimes-check Check real Clang type/construction bindings and 80% native/Python coverage' 'native-units-check Check explicit native build contexts and 80% component coverage' 'native-public-lifetimes-check Check common-policy C++ lifetime build/launch and 80% Python coverage' 'native-benchmark-check Check C/C++ stock/four-state timings, accounting and 80% coverage' 'java-build  Build the official Java-agent extension' 'java-context-examples Build ordinary task/HTTP examples and their prepared manifests' 'java-witness-bindings Compile exact offline diagnostic action bindings' 'stack-up    Start the separate rebuild Collector/Grafana stack' 'stack-down  Stop rebuild containers, retain data' 'benchmark   Compare plain and four live states; export results to Grafana' 'sonar-policy Verify the project Previous version policy'

setup:
	python3 -m venv .venv
	$(PYTHON) -m pip install --require-hashes --only-binary=:all: -r requirements-dev.txt -r requirements.txt
	PATH="$(NODE_PATH_PREFIX)$$PATH" npm ci --ignore-scripts --no-audit --cache "$(NPM_CACHE)"
	PATH="$(NODE_PATH_PREFIX)$$PATH" npm ci --ignore-scripts --no-audit --cache "$(NPM_CACHE)" --prefix ci/markdownlint

node-check: node-native-coverage node-http
	mkdir -p build
	OTELC_TEST_PYTHON="$(OTELC_TEST_PYTHON)" PATH="$(NODE_PATH_PREFIX)$$PATH" npm run check:node

node-observer:
	OTELC_NODE="$(NODE)" $(PYTHON) ci/build_node_observer.py

node-http:
	$(PYTHON) ci/build_node_http.py

node-native-coverage: node-observer
	OTELC_NODE="$(NODE)" $(PYTHON) ci/check_node_native_coverage.py

python-inputs:
	$(PYTHON) -m ci.build_python_inputs

python-inputs-check:
	$(PYTHON) -m ci.check_python_inputs_coverage

llvm-plugin:
	mkdir -p build
	$(PYTHON) -m otelc.llvm_backend --output "$(LLVM_OUTPUT)"

llvm-check:
	mkdir -p build
	$(PYTHON) -m coverage run --data-file=build/llvm-python.coverage --source=otelc.llvm_backend -m unittest discover -s tests -p 'test_llvm*.py' -v
	$(PYTHON) -m coverage report --data-file=build/llvm-python.coverage --fail-under=80

.PHONY: native-lifetimes-check
native-lifetimes-check:
	mkdir -p build
	$(PYTHON) -m coverage run --data-file=build/native-lifetimes-python.coverage --source=otelc.native_lifetimes -m unittest discover -s tests -p test_native_lifetimes.py -v
	$(PYTHON) -m coverage report --data-file=build/native-lifetimes-python.coverage --fail-under=80

.PHONY: native-units-check
native-units-check:
	mkdir -p build
	$(PYTHON) -m coverage run --data-file=build/native-units.coverage --source=otelc.native_units -m unittest discover -s tests -p test_native_units.py -v
	$(PYTHON) -m coverage report --data-file=build/native-units.coverage --fail-under=80

.PHONY: native-public-lifetimes-check
native-public-lifetimes-check:
	mkdir -p build
	$(PYTHON) -m coverage run --data-file=build/native-public-objects.coverage --source=otelc.native_object_build -m unittest discover -s tests -p test_native_public_lifetimes.py -v
	$(PYTHON) -m coverage report --data-file=build/native-public-objects.coverage --fail-under=80

.PHONY: native-benchmark-check
native-benchmark-check:
	mkdir -p build
	$(PYTHON) -m coverage run --data-file=build/native-benchmark.coverage --source=otelc.native_benchmark,otelc.native_benchmark_launch -m unittest discover -s tests -p test_native_benchmark.py -v
	$(PYTHON) -m coverage report --data-file=build/native-benchmark.coverage --include='*/otelc/native_benchmark.py' --fail-under=80
	$(PYTHON) -m coverage report --data-file=build/native-benchmark.coverage --include='*/otelc/native_benchmark_launch.py' --fail-under=80

native-check:
	mkdir -p build
	$(PYTHON) -m coverage run --data-file=build/native-tooling.coverage --source=ci.check_native_controls -m unittest discover -s tests -p test_native_controls_coverage.py -v
	$(PYTHON) -m coverage run --append --data-file=build/native-tooling.coverage --source=ci.check_native_controls -m ci.check_native_controls
	$(PYTHON) -m coverage report --data-file=build/native-tooling.coverage --fail-under=80

native-launch-sdk:
	mkdir -p build
	$(PYTHON) -m otelc.native_launch --output "$(NATIVE_LAUNCH_OUTPUT)"

native-launch-check:
	mkdir -p build
	$(PYTHON) -m coverage run --data-file=build/native-launch-tooling.coverage --source=ci.check_native_launch -m ci.check_native_launch
	$(PYTHON) -m coverage report --data-file=build/native-launch-tooling.coverage --fail-under=80
	$(PYTHON) -m coverage report --data-file=build/native-launch-python.coverage --include='*/otelc/native_launch.py' --fail-under=80
	$(PYTHON) -m coverage report --data-file=build/native-launch-python.coverage --include='*/ci/native_launch_suite.py' --fail-under=80

go-check:
	mkdir -p build
	cd adapters/go && test -z "$$(gofmt -l metriccontrol/*.go)"
	cd adapters/go && GOWORK=off $(GO) vet -mod=readonly ./...
	cd adapters/go && GOWORK=off $(GO) test -mod=readonly -race -count=1 -coverprofile="$(CURDIR)/build/go-coverage.out" ./...
	cd adapters/go && $(GO) tool cover -func="$(CURDIR)/build/go-coverage.out" | awk '/^total:/ {gsub("%", "", $$3); print; found=1; if ($$3 < 80) exit 1} END {if (!found) exit 1}'
	awk 'NR>1 {split($$1, p, ":"); total[p[1]]+=$$2; if ($$3>0) hit[p[1]]+=$$2} END {for (file in total) {files++; percent=100*hit[file]/total[file]; printf "%s: %.2f%% statements\n", file, percent; if (percent<80) failed=1} if (files!=3 || failed) exit 1}' build/go-coverage.out
	sed 's|^io\.quux\.otelc/go/|adapters/go/|' build/go-coverage.out > build/go-sonar-coverage.out

java-build:
	$(MAVEN) -q -f adapters/java/pom.xml -DskipTests package

java-check:
	$(MAVEN) -q -f adapters/java/pom.xml verify

java-context-examples: java-build
	$(PYTHON) -m otelc.java_examples

java-witness-bindings: java-build
	mkdir -p build/witness/java/classes
	javac -d build/witness/java/classes examples/java/BenchmarkApp.java examples/java/WitnessHarness.java
	java -cp 'adapters/java/target/java-extension-0.2.0-SNAPSHOT.jar:adapters/java/target/java-extension-0.2.0-SNAPSHOT-helper-tool.jar' io.quux.otelc.witness.WitnessBindings \
	  build/witness/java/classes/examples/WitnessHarness.class examples/WitnessHarness envelope '(I)J' \
	  build/witness/java/classes/examples/WitnessHarness.class examples/WitnessHarness rendezvous '()V' \
	  build/witness/java/classes/examples/WitnessHarness.class examples/WitnessHarness wrapper '(I)J' \
	  build/witness/java/classes/examples/BenchmarkApp.class examples/BenchmarkApp workload '(I)J' > build/witness/java/bindings.json

lint:
	ci/markdownlint/node_modules/.bin/markdownlint '**/*.md' --ignore '**/node_modules/**' --ignore '.venv/**' --ignore '**/target/**' --ignore 'build/**'
	$(PYTHON) -m compileall -q otelc ci tests

test: java-build node-observer node-http native-check native-launch-check
	mkdir -p build
	OTELC_NODE="$(NODE)" OTELC_TEST_NODE="$(NODE)" $(PYTHON) -m coverage run --source=otelc,ci -m unittest discover -s tests -v
	$(PYTHON) -m coverage combine --append --keep build/native-tooling.coverage build/native-launch-tooling.coverage build/native-launch-python.coverage build/native-prepared-subprocess.coverage
	$(PYTHON) -m coverage report --fail-under=80
	$(PYTHON) -m coverage report --include='*/ci/native_encoder.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/ci/native_origin_chain.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/python_origin_journal.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/python_origin_execution.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/llvm_backend.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/ci/native_sonar.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/native_gate.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/native_launch.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/native_build.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/native_units.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/native_object_build.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/native_benchmark.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/benchmark_cleanup.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/native_benchmark_launch.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/native_supervisor.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/prepared_native.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/native_lifetimes.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/ci/check_native_launch.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/ci/native_launch_suite.py' --fail-under=80
	$(PYTHON) -m coverage xml -o build/coverage.xml
	$(PYTHON) -m ci.native_sonar

check: lint java-check node-check go-check python-inputs-check test

stack-check:
	docker compose config --quiet

stack-up: stack-check
	docker compose up -d

stack-down:
	docker compose down

benchmark: $(if $(filter java,$(BENCHMARK_LANGUAGE)),java-build) $(if $(filter javascript typescript,$(BENCHMARK_LANGUAGE)),node-observer node-http)
	OTELC_PYTHON="$(PYTHON)" bin/quux-otelc --config "$(BENCHMARK_CONFIG)" benchmark --language $(BENCHMARK_LANGUAGE) --sdk-build "$(NATIVE_LAUNCH_OUTPUT)" --output build/benchmarks/$(BENCHMARK_LANGUAGE)-$$(date +%Y%m%d-%H%M%S)

sonar-policy:
	$(PYTHON) ci/verify_sonar_policy.py
