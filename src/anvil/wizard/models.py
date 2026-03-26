"""Data models for task creation wizard."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Task:
    """A single evaluation task."""

    instance_id: str
    problem_statement: str
    patch: str
    base_commit: str
    repo: str
    before_repo_set_cmd: str = ""
