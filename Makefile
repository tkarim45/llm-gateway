.PHONY: install test serve
install:
	pip install -e ".[dev]"
test:
	pytest -q
serve:
	uvicorn gateway.api:app --host 0.0.0.0 --port 8000
