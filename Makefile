PYTHON ?= .venv/bin/python

.PHONY: help ui-inventory ui-inventory-check

help:
	@echo "make ui-inventory        regenerate docs/ui-inventory.md from the code"
	@echo "make ui-inventory-check  fail if docs/ui-inventory.md is stale (CI)"

## Regenerate the user-facing capability inventory.
ui-inventory:
	$(PYTHON) scripts/gen_ui_inventory.py

## Verify the committed inventory matches the code.
ui-inventory-check:
	$(PYTHON) scripts/gen_ui_inventory.py --check
