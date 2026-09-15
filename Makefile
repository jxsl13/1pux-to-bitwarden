PYTHON ?= python3

.PHONY: test check

test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -v

check: test
	$(PYTHON) import_attachments.py --help >/dev/null
	$(PYTHON) start.py --help >/dev/null
	@if command -v zsh >/dev/null; then zsh -n Start.command; fi
