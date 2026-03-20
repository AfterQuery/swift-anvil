# Anvil Swift

A benchmark for evaluating LLM coding agents on real-world Swift/iOS tasks. Agents receive a problem statement, generate a patch, and are evaluated by compiling the project and running XCTest unit tests.

## How it works

1. **Agent phase**: Each task runs in a Modal sandbox using a pre-built Docker image. The agent receives the problem statement and generates a patch.

2. **Eval phase**: Patches are applied to a local worktree with cached DerivedData. `xcodebuild` compiles the patched project and runs per-task unit tests (from `tests.swift`). Each worker gets its own simulator clone to avoid boot conflicts during parallel evaluation.

3. **Output**: Trajectories, patches, stdout/stderr, and eval results are saved per-task. A summary with pass@k metrics is printed at the end.

````

## Setup

**1. Install dependencies and Xcode prerequisites**

```bash
make setup
````

**2. Configure environment**

`make setup` copies `.env.example` to `.env` automatically. Open `.env` and fill in:

- `OPENROUTER_API_KEY` (or whichever provider you're using)
- `REGISTRY_USERNAME` - your Docker Hub username
- `REGISTRY_PASSWORD` - a Docker Hub [access token](https://hub.docker.com/settings/security)

**3. Authenticate services**

Make sure Docker is running locally, then:

```bash
modal setup          # Modal account for sandboxed agent execution
docker login         # Docker Hub for image pulls
```

**4. Create a private Docker Hub repository**

Go to [hub.docker.com](https://hub.docker.com) and create a new **private** repository (e.g., `anvil-images`).

> ⚠️ Public repos will not work—Anvil refuses to push task images to public repositories to prevent data leakage.

## Usage

### Publish task images

Build and push Docker images for a dataset to your private repo:

```bash
anvil publish-images --dataset datasets/ACHNBrowserUI
```

The username and repo are read from `REGISTRY_USERNAME` and `REGISTRY_REPO` in `.env` (or pass `-u <username>` / `--repo <name>` to override).

Modal sandboxes pull images from Docker Hub, so task images need to be pushed there first.

To remove local anvil images: `docker rmi $(docker images $(grep REGISTRY_USERNAME .env | cut -d= -f2)/anvil-images -q) --force`

### Oracle Agent

Use the `oracle` agent to validate your task harnesses before running LLM agents:

```bash
anvil run-evals --dataset datasets/ACHNBrowserUI --agent oracle
```

The oracle agent skips LLM rollouts and applies gold patches from `gold_patches.json` directly. All tests should pass if your harness is correct.

Each dataset needs a `xcode_config.yaml` in its source tasks directory (e.g. `tasks/ACHNBrowserUI/xcode_config.yaml`) specifying the Xcode project/workspace, scheme, and build destination.

### Run evaluations

```bash
anvil run-evals \
  --dataset datasets/ACHNBrowserUI \
  --model openrouter/anthropic/claude-sonnet-4.5 \
  --agent mini-swe-agent \
  --n-attempts 4
```

Use `--n-attempts` to control how many runs per task (useful for pass@k metrics). Results are saved to `<dataset>/runs/<agent>_<model>/`.

> 💡 **Progress is saved automatically** to minimize costs. If you re-run the same command, completed tasks are skipped. Use `--no-continue` to start fresh.

### Options

| Flag                   | Default                 | Description                                         |
| ---------------------- | ----------------------- | --------------------------------------------------- |
| `--model`              | —                       | Model ID (required for agents, optional for oracle) |
| `--dataset`            | —                       | Dataset ID or path                                  |
| `--agent`              | mini-swe-agent          | Agent to use (`mini-swe-agent` or `oracle`)         |
| `--n-attempts`         | 1                       | Attempts per task (for pass@k)                      |
| `--compile-only`       | false                   | Only check compilation, skip unit tests             |
| `--no-continue`        | false                   | Start fresh, ignore previous results                |
| `--max-parallel`       | 30                      | Concurrent agent runs                               |
| `--max-wait`           | auto                    | Minutes to wait for Modal rate limits               |
| `--eval-backend`       | `xcode`                 | `xcode` (local macOS) or `modal` (Docker/Modal)     |
| `--dockerhub-username` | `REGISTRY_USERNAME` env | Docker Hub username (modal backend)                 |
| `--dockerhub-repo`     | `anvil-images`          | Docker Hub repo name (modal backend)                |

### GitHub Actions

Use the [Anvil Eval workflow](https://github.com/AfterQuery/anvil-swift/actions/workflows/eval.yml) to run evaluations in CI. Click **Run workflow**, pick a dataset, model, and agent from the dropdowns, then set the number of attempts. Results are uploaded as artifacts on the workflow run.
