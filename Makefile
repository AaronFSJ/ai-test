PYTHON ?= python3
DB ?= ./aitest.db

.PHONY: test test-t02 test-t08 test-t09 test-t16 evidence evidence-t01 evidence-t02 evidence-t08 evidence-t09 evidence-t16 migrate serve

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

test-t02:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -p 'test_t02.py' -v

test-t08:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -p 'test_t08.py' -v

test-t09:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -p 'test_t09.py' -v

test-t16:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -p 'test_t16.py' -v

evidence: evidence-t01 evidence-t02 evidence-t08 evidence-t09 evidence-t16

evidence-t01:
	PYTHONPATH=src $(PYTHON) scripts/verify_t01.py --out ../deliverables/evidence

evidence-t02:
	PYTHONPATH=src $(PYTHON) scripts/verify_t02.py --out ../deliverables/evidence

evidence-t08:
	PYTHONPATH=src $(PYTHON) scripts/verify_t08.py --out ../deliverables/evidence

evidence-t09:
	PYTHONPATH=src $(PYTHON) scripts/verify_t09.py --out ../deliverables/evidence

evidence-t16:
	PYTHONPATH=src $(PYTHON) scripts/verify_t16.py --out ../deliverables/evidence

migrate:
	PYTHONPATH=src $(PYTHON) -m aitest.cli migrate --db $(DB)

serve:
	PYTHONPATH=src $(PYTHON) -m aitest.cli serve --db $(DB) --port 8080
