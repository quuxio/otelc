PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
MARKDOWNLINT ?= $(if $(wildcard ci/markdownlint/node_modules/.bin/markdownlint),ci/markdownlint/node_modules/.bin/markdownlint,markdownlint)
SONAR_SCANNER ?= sonar-scanner

.PHONY: help setup lint test check sonar-policy scan

help:
	@printf '%s\n' 'setup         Install local validation dependencies' 'lint          Lint all Markdown' 'test          Run offline tests and enforce 90% coverage' 'check         Run lint and tests' 'sonar-policy  Verify the remote Previous version policy' 'scan          Analyze a clean commit and wait for its quality gate'

setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.txt
	npm ci --ignore-scripts --prefix ci/markdownlint

lint:
	$(MARKDOWNLINT) '**/*.md' --ignore '**/node_modules/**' --ignore '.venv/**' --ignore '.scannerwork/**'

test:
	mkdir -p build
	$(PYTHON) -m coverage run --source=ci -m unittest discover -s tests -v
	$(PYTHON) -m coverage report --fail-under=90
	$(PYTHON) -m coverage xml -o build/coverage.xml

check: lint test

sonar-policy:
	$(PYTHON) ci/verify_sonar_policy.py

scan: check sonar-policy
	@test -z "$$(git status --porcelain)" || { printf '%s\n' 'Commit changes before an authoritative scan.' >&2; exit 1; }
	$(SONAR_SCANNER) -Dsonar.projectVersion="$$(git rev-parse HEAD)" -Dsonar.qualitygate.wait=true
