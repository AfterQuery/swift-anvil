.PHONY: setup test format

setup:
	uv venv
	uv sync
	xcode-select --install || true
	@xcrun simctl list runtimes 2>/dev/null | grep -q com.apple.CoreSimulator.SimRuntime.iOS \
		|| xcodebuild -downloadPlatform iOS
	cp -n .env.example .env || true

test:
	uv sync --group dev
	uv run pytest tests/ -v

format:
	uv sync --group dev
	uv run ruff format .
