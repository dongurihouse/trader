PYTHON := /usr/bin/python3
LABEL := com.xup.bars
PLIST := $(HOME)/Library/LaunchAgents/$(LABEL).plist
UID := $(shell id -u)

.PHONY: install uninstall restart status logs once backfill query

install:
	@mkdir -p data logs "$(HOME)/Library/LaunchAgents"
	@cp launchd/$(LABEL).plist "$(PLIST)"
	@launchctl bootout gui/$(UID)/$(LABEL) >/dev/null 2>&1 || true
	@launchctl bootstrap gui/$(UID) "$(PLIST)"
	@echo "started $(LABEL)"

uninstall:
	@launchctl bootout gui/$(UID)/$(LABEL) >/dev/null 2>&1 || true
	@rm -f "$(PLIST)"
	@echo "stopped $(LABEL); data was kept"

restart:
	@launchctl kickstart -k gui/$(UID)/$(LABEL)

status:
	@launchctl print gui/$(UID)/$(LABEL) | sed -n '1,45p'
	@$(PYTHON) bars_service.py status

logs:
	@tail -f logs/service.log

once:
	@$(PYTHON) bars_service.py once

backfill:
	@$(PYTHON) bars_service.py backfill

query:
	@$(PYTHON) bars_service.py query $(SYMBOL) $(ARGS)
