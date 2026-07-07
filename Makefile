PYTHON ?= python3
PYTHONPATH := src
PORT ?= 8000

.PHONY: test site audit strategies help

help:
	@echo "APlan local commands:"
	@echo "  make test       Run the unit test suite from source"
	@echo "  make site       Serve docs/ at http://127.0.0.1:$(PORT)"
	@echo "  make audit      Verify the local audit hash chain"
	@echo "  make strategies List registered strategies"

test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests -v

site:
	$(PYTHON) -m http.server $(PORT) -d docs

audit:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m aplan.audit verify

strategies:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m aplan.strategy_cli list
