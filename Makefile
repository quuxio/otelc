PYTHON ?= .venv/bin/python
MAVEN ?= mvn
NODE ?= $(if $(CI),node,$(if $(wildcard /opt/homebrew/opt/node@24/bin/node),/opt/homebrew/opt/node@24/bin/node,node))
NODE_PATH_PREFIX = $(if $(findstring /,$(NODE)),$(dir $(NODE)):)
NPM_CACHE ?= $(CURDIR)/build/npm-cache

.PHONY: help setup check lint test node-check java-build java-check java-context-examples java-witness-bindings stack-up stack-down stack-check benchmark sonar-policy
.NOTPARALLEL: check

help:
	@printf '%s\n' 'setup       Install locked Python, Node SDK and Markdown tools' 'check       Run lint and Java/Node/Python tests with component coverage gates' 'node-check  Check Microsoft TypeScript 7 types and Node SDK/control regressions' 'java-build  Build the official Java-agent extension' 'java-context-examples Build ordinary task/HTTP examples and their prepared manifests' 'java-witness-bindings Compile offline diagnostic action bindings (no runtime witness yet)' 'stack-up    Start the separate rebuild Collector/Grafana stack' 'stack-down  Stop rebuild containers, retain data' 'benchmark   Compare plain and four live states; export results to Grafana' 'sonar-policy Verify the project Previous version policy'

setup:
	python3 -m venv .venv
	$(PYTHON) -m pip install --require-hashes --only-binary=:all: -r requirements-dev.txt -r requirements.txt
	PATH="$(NODE_PATH_PREFIX)$$PATH" npm ci --ignore-scripts --no-audit --cache "$(NPM_CACHE)"
	PATH="$(NODE_PATH_PREFIX)$$PATH" npm ci --ignore-scripts --no-audit --cache "$(NPM_CACHE)" --prefix ci/markdownlint

node-check:
	mkdir -p build
	PATH="$(NODE_PATH_PREFIX)$$PATH" npm run check:node

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

test: java-build
	mkdir -p build
	$(PYTHON) -m coverage run --source=otelc,ci -m unittest discover -s tests -v
	$(PYTHON) -m coverage report --fail-under=80
	$(PYTHON) -m coverage xml -o build/coverage.xml

check: lint java-check node-check test

stack-check:
	docker compose config --quiet

stack-up: stack-check
	docker compose up -d

stack-down:
	docker compose down

benchmark: java-build
	$(PYTHON) -m otelc benchmark --output build/benchmarks/run-$$(date +%Y%m%d-%H%M%S)

sonar-policy:
	$(PYTHON) ci/verify_sonar_policy.py
