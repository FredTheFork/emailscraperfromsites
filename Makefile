.PHONY: install test lint serve
install:
	python -m pip install -r requirements.lock
	python -m pip install -e '.[dev]'
test:
	python -m pytest --cov=publicmail --cov-report=term-missing
lint:
	python -m ruff check src tests notebooks/colab_client.py
	python -m ruff format --check src tests notebooks/colab_client.py
	node --check src/publicmail/static/app.js
serve:
	publicmail serve
