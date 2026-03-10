"""Data models for task creation wizard."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TestSpec:
    """Specification for test expectations."""

    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)

    @staticmethod
    def _format_test_list(tests: list[str]) -> str:
        return "[" + ", ".join(f"'{t}'" for t in tests) + "]"

    def to_fail_to_pass_str(self) -> str:
        """Format fail_to_pass as a string list for instance_info.txt."""
        return self._format_test_list(self.fail_to_pass)

    def to_pass_to_pass_str(self) -> str:
        """Format pass_to_pass as a string list for instance_info.txt."""
        return self._format_test_list(self.pass_to_pass)


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
    language: str = "Python"
    before_repo_set_cmd: str = ""
    requirements: str = ""
    interface: str = ""
    issue_specificity: str = ""
    issue_categories: str = ""


@dataclass
class Dataset:
    """A dataset containing multiple evaluation tasks."""

    dataset_id: str
    repo_path: Path | None = None
    repo_url: str | None = None
    base_image: str = "ubuntu:24.04"
    language: str = "python"
    tasks: list[Task] = field(default_factory=list)

    @property
    def repo_name(self) -> str:
        """Get the repository name from path or URL."""
        if self.repo_path:
            return self.repo_path.name
        if self.repo_url:
            # Extract repo name from URL like https://github.com/user/repo.git
            name = self.repo_url.rstrip("/").split("/")[-1]
            if name.endswith(".git"):
                name = name[:-4]
            return name
        return self.dataset_id

