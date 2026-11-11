PYTHON ?= python3
DB ?= ./aitest.db

.PHONY: test evidence evidence-t01 evidence-t08 evidence-t09 evidence-t12 migrate serve

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

evidence: evidence-t01 evidence-t08 evidence-t09 evidence-t12

evidence-t01:
	PYTHONPATH=src $(PYTHON) scripts/verify_t01.py --out ../deliverables/evidence

evidence-t08:
	PYTHONPATH=src $(PYTHON) scripts/verify_t08.py --out ../deliverables/evidence

evidence-t09:
	PYTHONPATH=src $(PYTHON) scripts/verify_t09.py --out ../deliverables/evidence

evidence-t12:
	PYTHONPATH=src $(PYTHON) scripts/verify_t12.py --out ../deliverables/evidence

migrate:
	PYTHONPATH=src $(PYTHON) -m aitest.cli migrate --db $(DB)

serve:
	PYTHONPATH=src $(PYTHON) -m aitest.cli serve --db $(DB) --port 8080
