PYTHON := bars/.venv/bin/python
UV := /Users/xup/.local/bin/uv
LABEL := com.xup.bars
PLIST := $(HOME)/Library/LaunchAgents/$(LABEL).plist
UID := $(shell id -u)

.PHONY: sync auth install uninstall restart status logs once backfill sweep query

sync:
	@$(UV) sync --project bars --frozen

auth: sync
	@$(PYTHON) bars/bars_service.py auth

install: sync
	@mkdir -p data "$(HOME)/Library/LaunchAgents"
	@cp config/$(LABEL).plist "$(PLIST)"
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
	@$(PYTHON) bars/bars_service.py status

logs:
	@sqlite3 -header -column data/trader.sqlite3 \
		"SELECT datetime(ts, 'unixepoch') AS utc, service, level, message FROM logs ORDER BY rowid DESC LIMIT 50"

once:
	@$(PYTHON) bars/bars_service.py once

backfill:
	@$(PYTHON) bars/bars_service.py backfill

sweep:
	@$(PYTHON) bars/bars_service.py sweep

query:
	@$(PYTHON) bars/bars_service.py query $(SYMBOL) $(ARGS)
