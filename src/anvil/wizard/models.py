"""Data models for task creation wizard."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Task:
    """A single evaluation task."""

    task_id: str
    instance_id: str
    problem_statement: str
    patch: str
    test_code: str
    base_commit: str
    repo: str
    language: str = "Swift"
    before_repo_set_cmd: str = ""
    requirements: str = ""
    interface: str = ""
    issue_specificity: str = ""
    issue_categories: str = ""
