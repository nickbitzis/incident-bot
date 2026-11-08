.PHONY: install test lint fmt simulate run screenshot docker clean

PORT ?= 8850

install:
	uv sync --python 3.12

test:
	uv run pytest -q

lint:
	uvx ruff check .

fmt:
	uvx ruff check --fix .

# The scripted quarter: 20 incidents. Fails unless every incident resolved with a full timeline.
simulate:
	uv run incidentbot simulate --check --out docs

run:
	uv run incidentbot serve --port $(PORT)

screenshot:
	uv run python scripts/screenshot.py --port $(PORT)

docker:
	docker build -t incidentbot:local .

clean:
	rm -rf .pytest_cache .ruff_cache data
