PYTHON ?= python3
DB ?= ./aitest.db

.PHONY: test evidence migrate serve

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

evidence:
	PYTHONPATH=src $(PYTHON) scripts/verify_t01.py --out ../deliverables/evidence

migrate:
	PYTHONPATH=src $(PYTHON) -m aitest.cli migrate --db $(DB)

serve:
	PYTHONPATH=src $(PYTHON) -m aitest.cli serve --db $(DB) --port 8080
