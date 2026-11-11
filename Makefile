PYTHON ?= python3
DB ?= ./aitest.db

.PHONY: test evidence evidence-t01 evidence-t03 migrate serve

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

evidence: evidence-t01 evidence-t03

evidence-t01:
	PYTHONPATH=src $(PYTHON) scripts/verify_t01.py --out ../deliverables/evidence

evidence-t03:
	PYTHONPATH=src $(PYTHON) scripts/verify_t03.py --out ../deliverables/evidence

migrate:
	PYTHONPATH=src $(PYTHON) -m aitest.cli migrate --db $(DB)

serve:
	PYTHONPATH=src $(PYTHON) -m aitest.cli serve --db $(DB) --port 8080
