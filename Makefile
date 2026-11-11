PYTHON ?= python3
DB ?= ./aitest.db

.PHONY: test test-t02 evidence evidence-t02 migrate serve

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

test-t02:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -p 'test_t02.py' -v

evidence:
	PYTHONPATH=src $(PYTHON) scripts/verify_t01.py --out ../deliverables/evidence

evidence-t02:
	PYTHONPATH=src $(PYTHON) scripts/verify_t02.py --out ../deliverables/evidence

migrate:
	PYTHONPATH=src $(PYTHON) -m aitest.cli migrate --db $(DB)

serve:
	PYTHONPATH=src $(PYTHON) -m aitest.cli serve --db $(DB) --port 8080
