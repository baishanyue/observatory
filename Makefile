PY ?= python3

.PHONY: test lint demo serve bench

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m compileall -q src tests benchmarks
	$(PY) -m pytest -q

demo:
	$(PY) -m observatory validate workspace/flows/research-agent.json
	$(PY) -m observatory summary workspace/runs/run_7fc21a9d04.json

serve:
	$(PY) -m observatory serve --port 8787

bench:
	$(PY) benchmarks/bench.py
