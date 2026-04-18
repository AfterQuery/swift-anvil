"""Shared LLM utilities for CLI commands."""

from __future__ import annotations

import os
import warnings

warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")

DEFAULT_MODEL = "openrouter/anthropic/claude-sonnet-4.6"


def llm_available() -> bool:
    """Check if any LLM API key is configured."""
    return bool(
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )


def strip_code_fences(text: str) -> str:
    """Remove markdown code fences if the LLM wraps its output in them."""
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.index("\n") if "\n" in stripped else len(stripped)
        stripped = stripped[first_newline + 1 :]
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


def call_llm(model: str, system: str, user: str) -> str | None:
    """Send a prompt via litellm. Returns None if no API key is available."""
    if not llm_available():
        return None
    import litellm

    resp = litellm.completion(
        model=model,
        max_tokens=8192,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    text = resp.choices[0].message.content
    if not text:
        return None
    return strip_code_fences(text)
