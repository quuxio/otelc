PYTHON ?= .venv/bin/python
OTELC_TEST_PYTHON ?= $(PYTHON)
MAVEN ?= mvn
GO ?= go
NODE ?= $(if $(CI),node,$(if $(wildcard /opt/homebrew/opt/node@24/bin/node),/opt/homebrew/opt/node@24/bin/node,node))
NODE_PATH_PREFIX = $(if $(findstring /,$(NODE)),$(dir $(NODE)):)
NPM_CACHE ?= $(CURDIR)/build/npm-cache
BENCHMARK_LANGUAGE ?= java
GO_BENCHMARK_BUILD ?= build/go-example

.PHONY: help setup check lint test go-check go-upstream node-check node-observer node-native-coverage node-http python-inputs python-inputs-check java-build java-check java-context-examples java-witness-bindings stack-up stack-down stack-check benchmark sonar-policy
.NOTPARALLEL: check

help:
	@printf '%s\n' 'setup       Install locked Python, Node SDK and Markdown tools' 'check       Run lint and Java/Node/Python/Go tests with component coverage gates' 'go-check    Run SDK gate race tests and require 80% Go statements overall and per file' 'go-upstream Build and verify the pinned upstream Go compiler wrapper' 'node-check  Check Microsoft compiler types and source/SDK/native coverage' 'node-observer Build the public-V8 observer for the selected Node version/ABI' 'node-http   Generate the source-bound official HTTP preservation product' 'python-inputs Build the candidate native CPython entry/import/code input component' 'python-inputs-check Profile native entry/import/code cases and require 80% per-file lines' 'java-build  Build the official Java-agent extension' 'java-context-examples Build ordinary task/HTTP examples and their prepared manifests' 'java-witness-bindings Compile exact offline diagnostic action bindings' 'stack-up    Start the separate rebuild Collector/Grafana stack' 'stack-down  Stop rebuild containers, retain data' 'benchmark   Compare plain and four live states; export results to Grafana' 'sonar-policy Verify the project Previous version policy'

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

go-upstream:
	$(PYTHON) -m ci.build_go_upstream --go "$(GO)"

go-check:
	mkdir -p build
	cd adapters/go && GOWORK=off $(GO) list -deps -mod=readonly ./... > "$(CURDIR)/build/go-dependencies.txt"
	awk '/^golang.org\/x\/crypto\/openpgp(\/|$$)/ {print "Unsupported unsafe Go dependency: " $$0; failed=1} END {exit failed}' build/go-dependencies.txt
	cd adapters/go && test -z "$$(gofmt -l metriccontrol/*.go pipeline/*.go probe/*.go runtime/*.go)"
	cd adapters/go && GOWORK=off $(GO) vet -mod=readonly ./...
	cd adapters/go && GOWORK=off $(GO) test -mod=readonly -race -count=1 -coverprofile="$(CURDIR)/build/go-coverage.out" ./...
	cd adapters/go && $(GO) tool cover -func="$(CURDIR)/build/go-coverage.out" | awk '/^total:/ {gsub("%", "", $$3); print; found=1; if ($$3 < 80) exit 1} END {if (!found) exit 1}'
	awk 'NR>1 {split($$1, p, ":"); total[p[1]]+=$$2; if ($$3>0) hit[p[1]]+=$$2} END {for (file in total) {files++; percent=100*hit[file]/total[file]; printf "%s: %.2f%% statements\n", file, percent; if (percent<80) failed=1} if (files!=16 || failed) exit 1}' build/go-coverage.out
	sed 's|^io\.quux\.otelc/go/|adapters/go/|' build/go-coverage.out > build/go-sonar-coverage.out
	cd adapters/go-source && test -z "$$(gofmt -l *.go)"
	cd adapters/go-source && GOWORK=off $(GO) vet -mod=readonly ./...
	cd adapters/go-source && GOWORK=off $(GO) test -mod=readonly -race -count=1 -coverprofile="$(CURDIR)/build/go-source-coverage.out" ./...
	cd adapters/go-source && $(GO) tool cover -func="$(CURDIR)/build/go-source-coverage.out" | awk '/^total:/ {gsub("%", "", $$3); print; found=1; if ($$3 < 80) exit 1} END {if (!found) exit 1}'
	sed '1d; s|^io\.quux\.otelc/go-source/|adapters/go-source/|' build/go-source-coverage.out >> build/go-sonar-coverage.out
	$(PYTHON) -m ci.check_go_http
	sed '1d; s|^io\.quux\.otelc/go-http/|adapters/go-http/|' build/go-http-coverage.out >> build/go-sonar-coverage.out

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

test: java-build node-observer node-http go-upstream
	mkdir -p build
	OTELC_NODE="$(NODE)" OTELC_TEST_NODE="$(NODE)" $(PYTHON) -m coverage run --source=otelc,ci -m unittest discover -s tests -v
	$(PYTHON) -m coverage report --fail-under=80
	$(PYTHON) -m coverage report --include='*/ci/native_encoder.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/ci/native_origin_chain.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/python_origin_journal.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/python_origin_execution.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/go_context_bundle.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/go_telemetry_bundle.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/go_health.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/go_runtime_config.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/go_source.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/go_build.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/prepared_go.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/go_supervisor.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/go_benchmark.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/otelc/go_http_bundle.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/ci/check_go_http.py' --fail-under=80
	$(PYTHON) -m coverage report --include='*/ci/build_go_upstream.py' --fail-under=80
	$(PYTHON) -m coverage xml -o build/coverage.xml

check: lint java-check node-check go-check python-inputs-check test

stack-check:
	docker compose config --quiet

stack-up: stack-check
	docker compose up -d

stack-down:
	docker compose down

$(GO_BENCHMARK_BUILD)/build.json:
	OTELC_PYTHON="$(PYTHON)" bin/quux-otelc go-build --root "$(CURDIR)/examples/go" --output "$(abspath $(GO_BENCHMARK_BUILD))" --go "$(GO)"

benchmark: $(if $(filter java,$(BENCHMARK_LANGUAGE)),java-build) $(if $(filter javascript typescript,$(BENCHMARK_LANGUAGE)),node-observer node-http) $(if $(filter go,$(BENCHMARK_LANGUAGE)),$(GO_BENCHMARK_BUILD)/build.json)
	OTELC_PYTHON="$(PYTHON)" bin/quux-otelc benchmark --language $(BENCHMARK_LANGUAGE) $(if $(filter go,$(BENCHMARK_LANGUAGE)),--build "$(abspath $(GO_BENCHMARK_BUILD))") --output build/benchmarks/$(BENCHMARK_LANGUAGE)-$$(date +%Y%m%d-%H%M%S)

sonar-policy:
	$(PYTHON) ci/verify_sonar_policy.py
