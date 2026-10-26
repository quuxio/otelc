PYTHON ?= python3
RECORD_TOOL := tools/qualification_record.py
RETENTION_COMMAND ?= $(PYTHON) -B tools/qualification_cleanup.py clean --apply

.PHONY: help qualification qualification-pack qualification-verify qualification-restore retention-check retention-clean check

help:
	@$(PYTHON) -B $(RECORD_TOOL) --help

# Put generated evidence in $$OTELC_QUALIFICATION_DIR inside COMMAND.
qualification:
	@test -n "$(ARCHIVE)" && test -n "$(COMMAND)"
	$(PYTHON) -B $(RECORD_TOOL) run "$(ARCHIVE)" -- $(COMMAND)

qualification-pack:
	@test -n "$(SOURCE)" && test -n "$(ARCHIVE)"
	$(PYTHON) -B $(RECORD_TOOL) pack "$(SOURCE)" "$(ARCHIVE)"

qualification-verify:
	@test -n "$(ARCHIVE)"
	$(PYTHON) -B $(RECORD_TOOL) verify "$(ARCHIVE)"

qualification-restore:
	@test -n "$(ARCHIVE)" && test -n "$(DESTINATION)"
	$(PYTHON) -B $(RECORD_TOOL) restore "$(ARCHIVE)" "$(DESTINATION)"

retention-check:
	$(PYTHON) -B tools/qualification_cleanup.py clean

# The caller selects a unique compressed audit filename for each scheduled run.
retention-clean:
	@test -n "$(ARCHIVE)"
	$(PYTHON) -B $(RECORD_TOOL) run "$(ARCHIVE)" -- $(RETENTION_COMMAND)

check:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -B -m unittest discover -s tools/tests -v
