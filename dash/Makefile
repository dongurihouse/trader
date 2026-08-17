LABEL := com.xup.trader-dash
PLIST_SOURCE := $(CURDIR)/$(LABEL).plist
PLIST_TARGET := $(HOME)/Library/LaunchAgents/$(LABEL).plist
UID := $(shell id -u)

.PHONY: install uninstall restart status

install:
	@mkdir -p "$(HOME)/Library/LaunchAgents"
	@cp "$(PLIST_SOURCE)" "$(PLIST_TARGET)"
	@launchctl bootout gui/$(UID)/$(LABEL) >/dev/null 2>&1 || true
	@launchctl bootstrap gui/$(UID) "$(PLIST_TARGET)"
	@echo "started $(LABEL) on 0.0.0.0:8790"

uninstall:
	@launchctl bootout gui/$(UID)/$(LABEL) >/dev/null 2>&1 || true
	@echo "stopped $(LABEL); dashboard files and data were kept"

restart:
	@launchctl kickstart -k gui/$(UID)/$(LABEL)

status:
	@launchctl print gui/$(UID)/$(LABEL) | sed -n '1,55p'
