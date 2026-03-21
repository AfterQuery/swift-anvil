.PHONY: setup test

setup:
	uv venv
	uv sync
	xcode-select --install || true
	xcodebuild -downloadPlatform iOS
	cp -n .env.example .env || true

test:
	uv sync --group dev
	uv run pytest tests/ -v
