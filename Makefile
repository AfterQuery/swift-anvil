.PHONY: setup

setup:
	uv venv
	uv sync
	xcode-select --install || true
	xcodebuild -downloadPlatform iOS
	cp -n .env.example .env || true
