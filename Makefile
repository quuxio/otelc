PYTHON ?= .venv/bin/python
MAVEN ?= mvn

.PHONY: help setup check lint test java-build java-check stack-up stack-down stack-check benchmark sonar-policy
.NOTPARALLEL: check

help:
	@printf '%s\n' 'setup       Install locked Python and Markdown tools' 'check       Run lint, Java tests/80% coverage and Python tests/80% coverage' 'java-build  Build the official Java-agent extension' 'stack-up    Start the separate rebuild Collector/Grafana stack' 'stack-down  Stop rebuild containers, retain data' 'benchmark   Compare plain and four live states; export results to Grafana' 'sonar-policy Verify the project Previous version policy'

setup:
	python3 -m venv .venv
	$(PYTHON) -m pip install --require-hashes --only-binary=:all: -r requirements-dev.txt -r requirements.txt
	npm ci --ignore-scripts --prefix ci/markdownlint

java-build:
	$(MAVEN) -q -f adapters/java/pom.xml -DskipTests package

java-check:
	$(MAVEN) -q -f adapters/java/pom.xml verify

lint:
	ci/markdownlint/node_modules/.bin/markdownlint '**/*.md' --ignore '**/node_modules/**' --ignore '.venv/**' --ignore '**/target/**' --ignore 'build/**'
	$(PYTHON) -m compileall -q otelc ci tests

test: java-build
	mkdir -p build
	$(PYTHON) -m coverage run --source=otelc,ci -m unittest discover -s tests -v
	$(PYTHON) -m coverage report --fail-under=80
	$(PYTHON) -m coverage xml -o build/coverage.xml

check: lint java-check test

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
