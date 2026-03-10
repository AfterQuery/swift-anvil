"""Generic agent harness for running coding agents on Modal.

This module provides a pluggable interface for running any CLI-based coding agent
(mini-swe-agent, claude-code, codex, grok, etc.) in Modal sandboxes with:
- Parallel execution across instances
- Unified git diff capture for patches
- stdout/stderr logging
- Agent trajectory capture
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Literal

import yaml

from ..config import tasks_dir as get_tasks_dir

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Configuration for a pluggable coding agent."""

    name: str  # e.g., "mini-swe-agent", "claude-code"
    install_cmd: str
    run_cmd: str  # Placeholders: {model}, {task}, {output_dir}
    output_format: Literal["trajectory_json", "git_only", "stdout"] = "git_only"
    timeout: int = 1200
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass
class AgentResult:
    """Result from running an agent on a single instance."""

    instance_id: str
    patch: str  # git diff output
    stdout: str
    stderr: str
    trajectory: dict | None
    exit_code: int
    duration_seconds: float
    error: str | None = None


# Predefined agent configurations
AGENT_CONFIGS: dict[str, AgentConfig] = {
    "mini-swe-agent": AgentConfig(
        name="mini-swe-agent",
        install_cmd="pip install -q mini-swe-agent || pip install -q --break-system-packages mini-swe-agent",
        run_cmd="mini -c mini.yaml -c /tmp/anvil_override.yaml --model {model} --task {task} --yolo --exit-immediately --output {output_dir}/trajectory.traj.json --cost-limit 0",
        output_format="trajectory_json",
        timeout=2400,
    ),
}


def _sq(s: str) -> str:
    """Shell-escape a string with single quotes."""
    return "'" + (s or "").replace("'", "'\"'\"'") + "'"


PATCH_START_MARKER = "===ANVIL_PATCH_START==="
PATCH_END_MARKER = "===ANVIL_PATCH_END==="
TRAJECTORY_START_MARKER = "===ANVIL_TRAJECTORY_START==="
TRAJECTORY_END_MARKER = "===ANVIL_TRAJECTORY_END==="


def _build_agent_script(
    agent_config: AgentConfig,
    instance: dict,
    model: str,
    provider_env_var: str,
) -> str:
    """Build the bash script to run inside the Modal sandbox."""
    task = instance.get("problem_statement", "")
    task += (
        "\n\n## Important constraints\n"
        "- Do NOT create, modify, or delete any test files or test cases. "
        "The existing test suite will be used to verify your implementation.\n"
        "\n## Efficiency constraint\n"
        "Start implementing changes within the first 5-8 steps. "
        "Keep analysis brief; prefer quick exploration followed by edits.\n"
    )

    if "pbxproj" in task.lower() or "xcode project" in task.lower():
        task += (
            "\n## Xcode project.pbxproj editing guide\n"
            "The `project.pbxproj` file is extremely fragile. Follow these rules exactly:\n"
            "1. **New source files** need THREE entries: a `PBXFileReference`, a `PBXBuildFile` "
            "(with `in Sources`), and the build-file UUID added to the correct "
            "`PBXSourcesBuildPhase/files` list. NEVER add `.swift` files to `PBXResourcesBuildPhase`.\n"
            "2. **Removing source files** requires deleting the corresponding `PBXFileReference`, "
            "`PBXBuildFile`, the UUID from `PBXSourcesBuildPhase/files`, and the entry from the "
            "parent `PBXGroup/children`.\n"
            "3. **UUIDs** must be exactly 24 hex characters (uppercase). Generate unique ones that "
            "don't collide with existing entries.\n"
            "4. **Group membership**: place file references in the correct `PBXGroup` matching the "
            "on-disk directory. Don't add files to `Preview Content` or unrelated groups.\n"
            "5. **Never corrupt the plist structure**: every `{` must have a matching `}`, every "
            "`(` a matching `)`. A single missing semicolon or brace makes the entire project "
            "unreadable.\n"
            "6. **Check the deployment target** before using newer APIs. Look for "
            "`IPHONEOS_DEPLOYMENT_TARGET` in the pbxproj to find the minimum iOS version.\n"
        )
    before_cmd = instance.get("before_repo_set_cmd", "")
    base_commit = instance.get("base_commit", "")
    output_dir = "/workspace/output"

    run_cmd = agent_config.run_cmd.format(
        model=_sq(model),
        task=_sq(task),
        output_dir=output_dir,
    )

    # Runtime reset to base commit as a safety net — no-op if the image is
    # already at the correct commit, but guards against image/commit drift.
    if base_commit:
        reset_cmds = [
            "cd /app",
            "if [ -d .git ]; then",
            f"    git reset --hard {base_commit} 2>/dev/null || true",
            f"    git checkout {base_commit} 2>/dev/null || true",
            "fi",
        ]
    else:
        reset_cmds = []

    lines = [
        "set -e",
        "export MSWEA_CONFIGURED=true",
        f"export MSWEA_MODEL_NAME={_sq(model)}",
        f"export MSWEA_MODEL_API_KEY={provider_env_var}",
        "export MSWEA_COST_TRACKING=ignore_errors",
        "export LITELLM_DROP_PARAMS=True",
        *reset_cmds,
        before_cmd if before_cmd else "true",
        "cd /app",
        "python3 -m ensurepip 2>/dev/null || true",
        "pip install --upgrade pip -q --break-system-packages 2>/dev/null || true",
        f"mkdir -p {output_dir}",
        agent_config.install_cmd,
        """cat > /tmp/anvil_override.yaml << 'ANVIL_CFG_EOF'
model:
  set_cache_control: null
agent:
  max_steps: 50
  max_thoughts: 100
  cost_limit: 0
ANVIL_CFG_EOF""",
        # Redirect stdin from /dev/null so that if mini-swe-agent hits cost/step
        # limits and drops to an interactive prompt (InteractiveAgent catches
        # LimitsExceeded and calls input()), it immediately gets EOF and exits
        # instead of hanging forever.
        f"{run_cmd} < /dev/null || true",
        """cat > .gitignore << 'GITIGNORE_EOF'
# === Build outputs ===
build/
dist/
out/
bin/
obj/
target/
.next/
.nuxt/
.output/
*.exe
*.dll
*.so
*.dylib
*.o
*.a

# === Dependencies ===
node_modules/
vendor/
.venv/
venv/
__pycache__/
*.pyc
*.pyo
*.egg-info/
.eggs/

# === Caches ===
.cache/
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.swp
*.swo
.DS_Store
Thumbs.db

# === IDE ===
.idea/
.vscode/
*.iml

# === Agent artifacts ===
# Test scripts and summaries agents create
test_*.sh
final_*.sh
verify_*.sh
*_test.sh
implementation_*.md
IMPLEMENTATION_*.md
*_summary.md

# === Conversion symlinks ===
# Created by anvil tasks convert - must not appear in patches
afterquery/

# === Docker/CI artifacts ===
Dockerfile
requirements.txt

# === This file itself ===
.gitignore
GITIGNORE_EOF
""",
        f'echo "{PATCH_START_MARKER}"',
        f"git add -A && git diff {base_commit if base_commit else 'HEAD'} -- . ':!afterquery/' ':!.gitignore' || true",
        f'echo "{PATCH_END_MARKER}"',
        f"echo '=== Files in {output_dir}:' && ls -la {output_dir}/ 2>/dev/null || echo '(none)'",
        f'echo "{TRAJECTORY_START_MARKER}"',
        f"cat {output_dir}/trajectory.traj.json 2>/dev/null || echo '{{}}'",
        f'echo "{TRAJECTORY_END_MARKER}"',
    ]

    return "\n".join(lines)


def _extract_between_markers(text: str, start: str, end: str) -> str:
    """Extract text between markers. Ensures trailing newline for git patches."""
    try:
        s = text.index(start) + len(start)
        result = text[s : text.index(end, s)].strip()
        return result + "\n" if result and not result.endswith("\n") else result
    except ValueError:
        return ""


async def run_agent_in_modal(
    agent_config: AgentConfig,
    instance: dict,
    model: str,
    provider_env_var: str,
    app: "modal.App",
    registry_secret: "modal.Secret | None" = None,
    on_running: Callable[[str], None] | None = None,
) -> AgentResult:
    """Execute an agent in a Modal sandbox for a single instance."""
    import modal

    instance_id = instance.get("instance_id", "unknown")
    image_name = instance.get("image_name", "")

    start_time = time.time()
    sandbox = None

    try:
        img = modal.Image.from_registry(
            image_name, secret=registry_secret, add_python="3.12"
        )
        script = _build_agent_script(agent_config, instance, model, provider_env_var)

        env_secrets = []
        env_var_name = provider_env_var.lstrip("$")
        api_key = os.environ.get(env_var_name)
        if api_key:
            env_secrets.append(modal.Secret.from_dict({env_var_name: api_key}))

        if agent_config.extra_env:
            env_secrets.append(modal.Secret.from_dict(agent_config.extra_env))

        sandbox = await modal.Sandbox.create.aio(
            "bash",
            "-lc",
            script,
            image=img,
            timeout=agent_config.timeout,
            app=app,
            secrets=env_secrets if env_secrets else None,
        )
        if on_running:
            on_running(instance_id)
        await sandbox.wait.aio()
        raw_stdout = await sandbox.stdout.read.aio()
        stderr = await sandbox.stderr.read.aio()
        exit_code = sandbox.returncode
        await sandbox.terminate.aio()

        patch = _extract_between_markers(
            raw_stdout, PATCH_START_MARKER, PATCH_END_MARKER
        )
        traj_str = _extract_between_markers(
            raw_stdout, TRAJECTORY_START_MARKER, TRAJECTORY_END_MARKER
        )

        trajectory = None
        if agent_config.output_format == "trajectory_json" and traj_str:
            try:
                parsed = json.loads(traj_str)
                if parsed and parsed != {}:
                    trajectory = parsed
            except json.JSONDecodeError:
                pass

        stdout = raw_stdout
        for start_m, end_m in [
            (PATCH_START_MARKER, PATCH_END_MARKER),
            (TRAJECTORY_START_MARKER, TRAJECTORY_END_MARKER),
        ]:
            try:
                s_idx = stdout.index(start_m)
                e_idx = stdout.index(end_m, s_idx) + len(end_m)
                stdout = stdout[:s_idx] + stdout[e_idx:]
            except ValueError:
                pass

        duration = time.time() - start_time

        logger.info(
            "Agent %s finished %s in %.1fs (exit_code=%s, patch=%d bytes, stdout=%d bytes, stderr=%d bytes)",
            agent_config.name,
            instance_id,
            duration,
            exit_code,
            len(patch),
            len(stdout),
            len(stderr),
        )

        return AgentResult(
            instance_id=instance_id,
            patch=patch,
            stdout=stdout.strip(),
            stderr=stderr,
            trajectory=trajectory,
            exit_code=exit_code if exit_code is not None else -1,
            duration_seconds=duration,
        )

    except Exception as e:
        duration = time.time() - start_time

        # Try to recover partial stdout/stderr from the sandbox on failure
        # (e.g., timeout). This gives us diagnostic output even when the
        # sandbox is killed before the script finishes.
        partial_stdout = ""
        partial_stderr = ""
        partial_patch = ""
        try:
            if sandbox is not None:
                partial_stdout = await sandbox.stdout.read.aio()
                partial_stderr = await sandbox.stderr.read.aio()
                partial_patch = _extract_between_markers(
                    partial_stdout, PATCH_START_MARKER, PATCH_END_MARKER
                )
                # Best-effort cleanup
                await sandbox.terminate.aio()
        except Exception:
            pass  # sandbox may already be dead

        error_msg = str(e)
        if "timeout" in error_msg.lower() or "Sandbox exceeded" in error_msg:
            error_msg = f"Sandbox timed out after {agent_config.timeout}s: {error_msg}"

        logger.warning(
            "Agent %s failed on %s after %.1fs: %s | partial_stdout=%d bytes, partial_stderr=%d bytes",
            agent_config.name,
            instance_id,
            duration,
            error_msg,
            len(partial_stdout),
            len(partial_stderr),
        )

        return AgentResult(
            instance_id=instance_id,
            patch=partial_patch,
            stdout=partial_stdout,
            stderr=partial_stderr,
            trajectory=None,
            exit_code=-1,
            duration_seconds=duration,
            error=error_msg,
        )


async def run_agents_batch(
    agent_config: AgentConfig,
    instances: list[dict],
    model: str,
    provider_env_var: str,
    on_progress: Callable[[str, str], None] | None = None,
    on_result: Callable[["AgentResult"], None] | None = None,
    max_wait_minutes: int = 20,
) -> list[AgentResult]:
    """Run agents on all instances."""
    import modal

    os.environ.setdefault("MODAL_MAX_THROTTLE_WAIT", str(max_wait_minutes * 60))

    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    except ImportError:
        pass

    app = modal.App.lookup("anvil-agent-harness", create_if_missing=True)

    registry_secret = None
    if os.environ.get("REGISTRY_USERNAME") and os.environ.get("REGISTRY_PASSWORD"):
        registry_secret = modal.Secret.from_dict(
            {
                "REGISTRY_USERNAME": os.environ["REGISTRY_USERNAME"],
                "REGISTRY_PASSWORD": os.environ["REGISTRY_PASSWORD"],
            }
        )

    async def run_one(instance: dict) -> AgentResult:
        instance_id = instance.get("instance_id", "unknown")
        if on_progress:
            on_progress(instance_id, "queued")

        def on_running(iid: str) -> None:
            if on_progress:
                on_progress(iid, "running")

        result = await run_agent_in_modal(
            agent_config=agent_config,
            instance=instance,
            model=model,
            provider_env_var=provider_env_var,
            app=app,
            registry_secret=registry_secret,
            on_running=on_running,
        )

        if on_progress:
            if result.exit_code == 0 and not result.error:
                on_progress(instance_id, "completed")
            else:
                error_preview = (result.error or f"exit_code={result.exit_code}")[:80]
                on_progress(instance_id, f"failed: {error_preview}")

        if on_result:
            on_result(result)

        return result

    tasks = []
    for inst in instances:
        tasks.append(asyncio.create_task(run_one(inst)))
        await asyncio.sleep(1.0)

    return await asyncio.gather(*tasks)


def write_single_result(
    result: AgentResult,
    output_dir: Path,
    eval_id: str,
) -> None:
    """Write a single agent result to disk."""
    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "stdout.log").write_text(result.stdout)
    (output_dir / "stderr.log").write_text(result.stderr)

    if result.trajectory:
        (output_dir / "trajectory.json").write_text(
            json.dumps(result.trajectory, indent=2)
        )

    pred_file = output_dir / f"{result.instance_id}.pred"
    pred_data = {
        "model_name_or_path": "results",
        "instance_id": result.instance_id,
        "model_patch": result.patch,
    }
    pred_file.write_text(json.dumps(pred_data))

    meta = {
        "instance_id": result.instance_id,
        "exit_code": result.exit_code,
        "duration_seconds": result.duration_seconds,
        "error": result.error,
    }
    (output_dir / "metadata.json").write_text(json.dumps(meta, indent=2))


def write_results(
    results: list[AgentResult],
    output_dir: Path,
    eval_id: str,
    attempt: int = 1,
) -> None:
    """Write agent results to disk in SWE-bench Pro compatible format."""
    patches = []

    for result in results:
        result_dir = output_dir / result.instance_id / f"attempt_{attempt}"
        write_single_result(result, result_dir, eval_id)

        patches.append(
            {
                "instance_id": result.instance_id,
                "patch": result.patch,
                "prefix": eval_id,
            }
        )

    patches_file = output_dir / f"{eval_id}_patches.json"
    patches_file.write_text(json.dumps(patches, indent=2))


def load_instances(dataset_id: str) -> list[dict]:
    """Load instances from dataset's instances.yaml."""
    inst_path = get_tasks_dir(dataset_id) / "instances.yaml"
    if not inst_path.exists():
        raise FileNotFoundError(f"instances.yaml not found at {inst_path}")

    instances = yaml.safe_load(inst_path.read_text())
    if not instances:
        raise ValueError(f"No instances found in {inst_path}")

    return instances


def migrate_pred_files(results_dir: Path, dry_run: bool = False) -> dict:
    """Migrate .pred files from raw diff format to JSON format."""
    stats = {"migrated": 0, "skipped": 0, "errors": []}

    if not results_dir.exists():
        stats["errors"].append(f"Results directory not found: {results_dir}")
        return stats

    for inst_dir in sorted(results_dir.iterdir()):
        if not inst_dir.is_dir():
            continue

        instance_id = inst_dir.name

        attempt_dirs = [
            d
            for d in inst_dir.iterdir()
            if d.is_dir() and d.name.startswith("attempt_")
        ]

        if attempt_dirs:
            for attempt_dir in sorted(attempt_dirs):
                pred_file = attempt_dir / f"{instance_id}.pred"
                _migrate_single_pred(pred_file, instance_id, dry_run, stats)
        else:
            pred_file = inst_dir / f"{instance_id}.pred"
            _migrate_single_pred(pred_file, instance_id, dry_run, stats)

    return stats


def _migrate_single_pred(
    pred_file: Path, instance_id: str, dry_run: bool, stats: dict
) -> None:
    """Migrate a single .pred file from raw diff to JSON format."""
    if not pred_file.exists():
        return

    try:
        content = pred_file.read_text()

        try:
            data = json.loads(content)
            if isinstance(data, dict) and "model_patch" in data:
                stats["skipped"] += 1
                return
        except json.JSONDecodeError:
            pass

        pred_data = {
            "model_name_or_path": "results",
            "instance_id": instance_id,
            "model_patch": content,
        }

        if not dry_run:
            pred_file.write_text(json.dumps(pred_data))

        stats["migrated"] += 1

    except Exception as e:
        stats["errors"].append(f"{instance_id}: {e}")
