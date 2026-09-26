install:
	python -m pip install -r requirements.txt

test:
	pytest -q

run:
	python run.py

benchmark:
	python scripts/benchmark.py
