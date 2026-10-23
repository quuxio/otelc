PYTHON ?= .venv/bin/python
OTELC_TEST_PYTHON ?= $(PYTHON)
MAVEN ?= mvn
NODE ?= $(if $(CI),node,$(if $(wildcard /opt/homebrew/opt/node@24/bin/node),/opt/homebrew/opt/node@24/bin/node,node))
NODE_PATH_PREFIX = $(if $(findstring /,$(NODE)),$(dir $(NODE)):)
NPM_CACHE ?= $(CURDIR)/build/npm-cache
BENCHMARK_LANGUAGE ?= java

.PHONY: help setup check lint test node-check node-observer node-native-coverage node-http python-inputs python-inputs-check java-build java-check java-context-examples java-witness-bindings stack-up stack-down stack-check benchmark sonar-policy
.NOTPARALLEL: check

help:
	@printf '%s\n' 'setup       Install locked Python, Node SDK and Markdown tools' 'check       Run lint and Java/Node/Python tests with component coverage gates' 'node-check  Check Microsoft compiler types and source/SDK/native coverage' 'node-observer Build the public-V8 observer for the selected Node version/ABI' 'node-http   Generate the source-bound official HTTP preservation product' 'python-inputs Build the candidate native CPython entry/import/code input component' 'python-inputs-check Profile native entry/import/code cases and require 80% per-file lines' 'java-build  Build the official Java-agent extension' 'java-context-examples Build ordinary task/HTTP examples and their prepared manifests' 'java-witness-bindings Compile offline diagnostic action bindings (no runtime witness yet)' 'stack-up    Start the separate rebuild Collector/Grafana stack' 'stack-down  Stop rebuild containers, retain data' 'benchmark   Compare plain and four live states; export results to Grafana' 'sonar-policy Verify the project Previous version policy'

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
	$(PYTHON) ci/build_python_inputs.py

python-inputs-check:
	$(PYTHON) -m ci.check_python_inputs_coverage

java-build:
	$(MAVEN) -q -f adapters/java/pom.xml -DskipTests package

java-check:
	$(MAVEN) -q -f adapters/java/pom.xml verify

java-context-examples: java-build
	$(PYTHON) -m otelc.java_examples

java-witness-bindings: java-build
	$(MAVEN) -q -f adapters/java/pom.xml org.apache.maven.plugins:maven-dependency-plugin:3.11.0:copy-dependencies -DincludeScope=test -DoutputDirectory=target/dependency
	mkdir -p build/witness/java/classes
	javac -d build/witness/java/classes examples/java/BenchmarkApp.java examples/java/WitnessHarness.java
	java -cp 'adapters/java/target/classes:adapters/java/target/dependency/*' io.quux.otelc.witness.WitnessBindings \
	  build/witness/java/classes/examples/WitnessHarness.class examples/WitnessHarness envelope '(I)J' \
	  build/witness/java/classes/examples/WitnessHarness.class examples/WitnessHarness rendezvous '()V' \
	  build/witness/java/classes/examples/WitnessHarness.class examples/WitnessHarness wrapper '(I)J' \
	  build/witness/java/classes/examples/BenchmarkApp.class examples/BenchmarkApp workload '(I)J' > build/witness/java/bindings.json

lint:
	ci/markdownlint/node_modules/.bin/markdownlint '**/*.md' --ignore '**/node_modules/**' --ignore '.venv/**' --ignore '**/target/**' --ignore 'build/**'
	$(PYTHON) -m compileall -q otelc ci tests

test: java-build node-observer node-http
	mkdir -p build
	OTELC_NODE="$(NODE)" OTELC_TEST_NODE="$(NODE)" $(PYTHON) -m coverage run --source=otelc,ci -m unittest discover -s tests -v
	$(PYTHON) -m coverage report --fail-under=80
	$(PYTHON) -m coverage xml -o build/coverage.xml

check: lint java-check node-check python-inputs-check test

stack-check:
	docker compose config --quiet

stack-up: stack-check
	docker compose up -d

stack-down:
	docker compose down

benchmark: $(if $(filter java,$(BENCHMARK_LANGUAGE)),java-build) $(if $(filter javascript typescript,$(BENCHMARK_LANGUAGE)),node-observer node-http)
	OTELC_PYTHON="$(PYTHON)" bin/quux-otelc benchmark --language $(BENCHMARK_LANGUAGE) --output build/benchmarks/$(BENCHMARK_LANGUAGE)-$$(date +%Y%m%d-%H%M%S)

sonar-policy:
	$(PYTHON) ci/verify_sonar_policy.py
