PYTHON ?= python3

.PHONY: test check

test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest -v test_import_attachments.py

check: test
	$(PYTHON) import_attachments.py --help >/dev/null
	zsh -n Start.command
