"""Main evaluation runner for anvil.

This module orchestrates running agents on datasets and evaluating their output.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

import typer
from tqdm import tqdm

from ..agents.harness import (
    AGENT_CONFIGS,
    AgentResult,
    load_instances,
    run_agent_in_modal,
    write_single_result,
)
from ..config import eval_dir, tasks_dir
from ..util import ensure_dir, model_id_from_model, provider_env_var_from_model
from .pass_at_k import (
    compute_pass_at_k_summary,
    print_pass_at_k_summary,
    save_pass_at_k_json,
)


def _fmt_s(seconds: float) -> str:
    """Format a duration as ``'42s (0.7m)'``."""
    return f"{seconds:.0f}s ({seconds / 60:.1f}m)"


def _eval_id(agent: str, model: str, *, run_ui_tests: bool = True) -> str:
    """Compose eval_id as '<agent>_<model-suffix>', optional '_unit-only' when UI tests are off."""
    if agent == "oracle":
        base = agent
    else:
        mid = model_id_from_model(model)
        base = f"{agent}_{mid}" if agent else mid
    if not run_ui_tests:
        base = f"{base}_unit-only"
    return base


def _get_completed_attempts(
    base_out: Path,
    instances: list[dict],
    k: int,
    rel_path: str,
    valid: Callable[[dict], bool],
) -> set[tuple[str, int]]:
    """Return set of (instance_id, attempt) pairs whose JSON file at rel_path passes valid()."""
    completed = set()
    for inst in instances:
        iid = inst["instance_id"]
        for attempt in range(1, k + 1):
            path = base_out / iid / f"attempt_{attempt}" / rel_path
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                    if valid(data):
                        completed.add((iid, attempt))
                except (json.JSONDecodeError, OSError):
                    pass
    return completed


def _get_completed_rollouts(
    base_out: Path, instances: list[dict], k: int
) -> set[tuple[str, int]]:
    """Return set of (instance_id, attempt) pairs that have valid completed rollouts."""
    return _get_completed_attempts(
        base_out, instances, k,
        rel_path="rollout/metadata.json",
        valid=lambda m: m.get("exit_code") == 0 and m.get("error") is None,
    )


def _get_completed_evals(
    base_out: Path, instances: list[dict], k: int
) -> set[tuple[str, int]]:
    """Return set of (instance_id, attempt) pairs that have valid completed evals."""
    return _get_completed_attempts(
        base_out, instances, k,
        rel_path="eval_results/eval_results.json",
        valid=lambda _: True,
    )


def _move_to_errors(src: Path, base_out: Path, errors_dir: Path) -> None:
    """Move src directory into errors_dir (mirroring its path relative to base_out)."""
    dst = errors_dir / src.relative_to(base_out)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.move(str(src), str(dst))


def _cleanup_bad_rollouts(base_out: Path, instances: list[dict], k: int) -> int:
    """Move bad rollouts to __errors/ folder. Returns count moved."""
    errors_dir = base_out / "__errors"
    moved = 0
    for inst in instances:
        iid = inst["instance_id"]
        for attempt in range(1, k + 1):
            attempt_dir = base_out / iid / f"attempt_{attempt}"
            meta_path = attempt_dir / "rollout" / "metadata.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                    if meta.get("exit_code") != 0 or meta.get("error") is not None:
                        _move_to_errors(attempt_dir, base_out, errors_dir)
                        moved += 1
                except (json.JSONDecodeError, OSError):
                    pass
    return moved


def _cleanup_bad_evals(base_out: Path, instances: list[dict], k: int) -> int:
    """Move bad eval results to __errors/ folder. Returns count moved."""
    errors_dir = base_out / "__errors"
    moved = 0
    for inst in instances:
        iid = inst["instance_id"]
        for attempt in range(1, k + 1):
            eval_results_dir = base_out / iid / f"attempt_{attempt}" / "eval_results"
            if eval_results_dir.exists() and not (eval_results_dir / "eval_results.json").exists():
                _move_to_errors(eval_results_dir, base_out, errors_dir)
                moved += 1
    return moved


def run_evaluation(
    model: str | None,
    dataset_id: str,
    agent: str = "mini-swe-agent",
    n_attempts: int = 1,
    output: str | None = None,
    max_wait_minutes: int | None = None,
    max_parallel: int = 30,
    no_continue: bool = False,
    compile_only: bool = False,
    rollout_only: bool = False,
    task_filter: list[str] | None = None,
    run_ui_tests: bool = True,
) -> int:
    """Run full evaluation with an agent on a dataset."""
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    except ImportError:
        pass

    # Oracle agent doesn't need a model
    if agent == "oracle":
        model = model or "oracle"
    elif not model:
        typer.echo("Error: --model is required for non-oracle agents")
        return 1

    # Validate registry credentials upfront - required for agent runs
    reg_user = os.environ.get("REGISTRY_USERNAME")
    reg_pass = os.environ.get("REGISTRY_PASSWORD")
    if agent != "oracle" and (not reg_user or not reg_pass):
        typer.echo("Error: REGISTRY_USERNAME and REGISTRY_PASSWORD must be set")
        typer.echo("")
        typer.echo("These credentials are required to pull Docker images from Docker Hub.")
        typer.echo("Set them in your environment or in a .env file at the repo root:")
        typer.echo("")
        typer.echo("  export REGISTRY_USERNAME=your_dockerhub_username")
        typer.echo("  export REGISTRY_PASSWORD=your_dockerhub_access_token")
        typer.echo("")
        typer.echo("You can create an access token at https://hub.docker.com/settings/security")
        return 1

    k = n_attempts
    
    # Validate k
    if k < 1:
        typer.echo("Error: --n-attempts must be at least 1")
        return 1

    # Default max wait = 10 minutes * k / 2 (e.g., k=5 -> 25 min)
    if max_wait_minutes is None:
        max_wait_minutes = max(10, 10 * k // 2)

    start_time = time.time()
    eval_id = _eval_id(agent, model, run_ui_tests=run_ui_tests)
    base_out_path = Path(output) if output else eval_dir(dataset_id, eval_id)
    
    # Handle --no-continue: delete existing results directory
    if no_continue and base_out_path.exists():
        shutil.rmtree(base_out_path)
        typer.echo(f"Deleted existing results: {base_out_path}")
    
    base_out = ensure_dir(base_out_path)
    instances = load_instances(dataset_id)

    if task_filter:
        # Accept full instance IDs or short suffixes (e.g. "task-7" matches "ACHNBrowserUI.task-7")
        def _matches(iid: str) -> bool:
            return any(iid == f or iid.endswith(f".{f}") or iid.endswith(f) for f in task_filter)
        instances = [i for i in instances if _matches(i["instance_id"])]
        if not instances:
            typer.echo(f"Error: no instances matched filter: {task_filter}", err=True)
            return 1

    n_tasks = len(instances)
    dataset_tasks_dir = tasks_dir(dataset_id)

    typer.echo(f"Running {agent} evaluation on {dataset_id}")
    typer.echo(f"  Model: {model}")
    typer.echo(f"  Tasks: {n_tasks}" + (f" (filtered: {[i['instance_id'] for i in instances]})" if task_filter else ""))
    typer.echo(f"  Attempts: {k}")
    typer.echo(f"  UI tests: {'on' if run_ui_tests else 'off (unit tests only)'}")
    typer.echo(f"  Output: {base_out}")

    # ---- Oracle: skip rollout, use gold_patches.json directly ----
    if agent == "oracle":
        gold_patches_path = dataset_tasks_dir / "gold_patches.json"
        if not gold_patches_path.exists():
            typer.echo(f"Error: gold_patches.json not found at {gold_patches_path}")
            return 1

        gold_patches = json.loads(gold_patches_path.read_text())
        typer.echo(f"Loaded {len(gold_patches)} golden patches")

        # Build patches for eval, add attempt=1
        bad_eval_moved = _cleanup_bad_evals(base_out, instances, k)
        completed_evals = _get_completed_evals(base_out, instances, k)

        instance_ids = {inst["instance_id"] for inst in instances}
        all_patches = []
        for p in gold_patches:
            iid = p["instance_id"]
            if iid not in instance_ids:
                continue
            if (iid, 1) not in completed_evals:
                all_patches.append({
                    "instance_id": iid,
                    "patch": p.get("patch", ""),
                    "prefix": eval_id,
                    "attempt": 1,
                })
    else:
        # ---- Non-oracle: run agent rollouts ----
        bad_moved = _cleanup_bad_rollouts(base_out, instances, k)
        completed_rollouts = _get_completed_rollouts(base_out, instances, k)

        work_items: list[tuple[dict, int]] = []
        for inst in instances:
            iid = inst["instance_id"]
            for attempt in range(1, k + 1):
                if (iid, attempt) not in completed_rollouts:
                    work_items.append((inst, attempt))

        total_runs = n_tasks * k
        remaining_runs = len(work_items)
        complete_runs = total_runs - remaining_runs

        if remaining_runs == 0:
            typer.echo(f"Rollouts: {complete_runs}/{total_runs} complete, nothing to run")
        else:
            status = f"Rollouts: {complete_runs}/{total_runs} complete, running {remaining_runs}"
            if bad_moved > 0:
                status += f" ({bad_moved} bad moved to __errors/)"
            typer.echo(status)

            agent_config = AGENT_CONFIGS[agent]
            provider_env = provider_env_var_from_model(model)
            keep_n = min(k, 10)

            results_by_instance: dict[str, list[AgentResult | None]] = {
                i["instance_id"]: [None] * k for i in instances
            }

            async def run_all_agents():
                import modal

                modal.enable_output()
                os.environ.setdefault("MODAL_MAX_THROTTLE_WAIT", str(max_wait_minutes * 60))

                app = modal.App.lookup("anvil-agent-harness", create_if_missing=True)

                registry_secret = None
                if os.environ.get("REGISTRY_USERNAME") and os.environ.get("REGISTRY_PASSWORD"):
                    registry_secret = modal.Secret.from_dict({
                        "REGISTRY_USERNAME": os.environ["REGISTRY_USERNAME"],
                        "REGISTRY_PASSWORD": os.environ["REGISTRY_PASSWORD"],
                    })

                semaphore = asyncio.Semaphore(max_parallel)
                pbar = tqdm(total=remaining_runs, desc="Agent runs", unit="run", file=sys.stderr)

                async def run_one(inst: dict, attempt: int) -> AgentResult:
                    async with semaphore:
                        result = await run_agent_in_modal(
                            agent_config=agent_config,
                            instance=inst,
                            model=model,
                            provider_env_var=provider_env,
                            app=app,
                            registry_secret=registry_secret,
                        )

                        iid = result.instance_id
                        results_by_instance[iid][attempt - 1] = result

                        if attempt <= keep_n:
                            result_dir = base_out / iid / f"attempt_{attempt}" / "rollout"
                            write_single_result(result, result_dir, eval_id)

                        status = "ok" if result.exit_code == 0 and not result.error else "fail"
                        pbar.set_postfix_str(f"{iid}:{attempt} {status}")
                        pbar.update(1)

                        return result

                tasks = [
                    asyncio.create_task(run_one(inst, attempt))
                    for inst, attempt in work_items
                ]
                await asyncio.gather(*tasks)
                pbar.close()

            typer.echo(f"Running agents (max {max_parallel} parallel)...")
            agent_start = time.time()
            asyncio.run(run_all_agents())
            agent_elapsed = time.time() - agent_start
            typer.echo(f"Agent runs complete in {_fmt_s(agent_elapsed)}")

        if rollout_only:
            typer.echo("Rollout-only mode: skipping evaluation phase.")
            return 0

        # ---- Evaluation Phase for non-oracle ----
        bad_eval_moved = _cleanup_bad_evals(base_out, instances, k)
        completed_evals = _get_completed_evals(base_out, instances, k)

        all_patches = []
        for inst in instances:
            iid = inst["instance_id"]
            for attempt in range(1, k + 1):
                if (iid, attempt) in completed_evals:
                    continue

                pred_path = base_out / iid / f"attempt_{attempt}" / "rollout" / f"{iid}.pred"
                patch = ""
                if pred_path.exists():
                    try:
                        pred_data = json.loads(pred_path.read_text())
                        patch = pred_data.get("model_patch", "")
                    except (json.JSONDecodeError, OSError):
                        pass

                all_patches.append({
                    "instance_id": iid,
                    "patch": patch,
                    "prefix": eval_id,
                    "attempt": attempt,
                })

    total_evals = n_tasks * k
    remaining_evals = len(all_patches)
    complete_evals = total_evals - remaining_evals

    if remaining_evals == 0:
        typer.echo(f"Evals: {complete_evals}/{total_evals} complete, nothing to run")
    else:
        eval_status = f"Evals: {complete_evals}/{total_evals} complete, running {remaining_evals}"
        if bad_eval_moved > 0:
            eval_status += f" ({bad_eval_moved} bad moved to __errors/)"
        typer.echo(eval_status)

    if all_patches:
        from .xcode_eval import run_xcode_evals

        eval_start = time.time()
        run_xcode_evals(
            patches=all_patches,
            instances=instances,
            dataset_tasks_dir=dataset_tasks_dir,
            output_dir=base_out,
            eval_id=eval_id,
            compile_only=compile_only,
            dataset_id=dataset_id,
            run_ui_tests=run_ui_tests,
        )
        eval_elapsed = time.time() - eval_start
        typer.echo(f"Xcode evals complete in {_fmt_s(eval_elapsed)}")

    # ---- Aggregate Results ----
    results_file = base_out / "eval_results.json"
    all_results = json.loads(results_file.read_text()) if results_file.exists() else {}

    eval_results: dict[str, list[bool]] = {i["instance_id"]: [] for i in instances}
    for inst in instances:
        iid = inst["instance_id"]
        for attempt in range(1, k + 1):
            key = f"{iid}:attempt_{attempt}"
            if key in all_results:
                eval_results[iid].append(all_results[key])
            else:
                task_result_path = (
                    base_out / iid / f"attempt_{attempt}" / "eval_results" / "eval_results.json"
                )
                if task_result_path.exists():
                    try:
                        task_result = json.loads(task_result_path.read_text())
                        eval_results[iid].append(task_result.get(iid, False))
                    except (json.JSONDecodeError, OSError):
                        eval_results[iid].append(False)
                else:
                    eval_results[iid].append(False)

    # Report per-attempt results
    for attempt in range(1, k + 1):
        passed = sum(
            1 for r in eval_results.values() if len(r) >= attempt and r[attempt - 1]
        )
        typer.echo(f"  Attempt {attempt}: {passed}/{n_tasks} passed")

    total_elapsed = time.time() - start_time
    typer.echo(f"Total time: {_fmt_s(total_elapsed)}")

    summary = compute_pass_at_k_summary(
        eval_results, model, dataset_id, agent, k, total_elapsed
    )
    print_pass_at_k_summary(summary)
    save_pass_at_k_json(summary, base_out / "eval_results_pass_at_k.json")
    
    return 0 if any(r.solved for r in summary.per_instance) else 1
