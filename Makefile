.PHONY: install test lint serve
install:
	python -m pip install -r requirements.lock
	python -m pip install -e '.[dev]'
test:
	python -m pytest --cov=publicmail --cov-report=term-missing
lint:
	python -m ruff check src tests
	python -m ruff format --check src tests
	node --check src/publicmail/static/app.js
serve:
	publicmail serve
