"""Data models for task creation wizard."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TestSpec:
    """Specification for test expectations."""

    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)


@dataclass
class Task:
    """A single evaluation task."""

    task_id: str
    instance_id: str
    problem_statement: str
    patch: str
    test_code: str
    test_spec: TestSpec
    base_commit: str
    repo: str
    language: str = "Swift"
    before_repo_set_cmd: str = ""
    requirements: str = ""
    interface: str = ""
    issue_specificity: str = ""
    issue_categories: str = ""
