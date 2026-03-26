# Anvil Swift

A benchmark for evaluating LLM coding agents on real-world Swift/iOS tasks. Agents receive a problem statement, generate a patch, and are evaluated by compiling the project and running XCTest unit tests.

## How it works

1. **Agent phase**: Each task runs in a Modal sandbox using a pre-built Docker image. The agent receives the problem statement and generates a patch.

2. **Eval phase**: Patches are applied to a local worktree with cached DerivedData. `xcodebuild` compiles the patched project and runs unit tests. Each worker gets its own simulator clone to avoid boot conflicts during parallel evaluation.

3. **Output**: Trajectories, patches, stdout/stderr, and eval results are saved per-task. A summary with pass@k metrics is printed at the end.

## Setup

1. Install dependencies and Xcode prerequisites

```bash
make setup
```

2. Configure environment

`make setup` copies `.env.example` to `.env` automatically. Open `.env` and fill in:

- `OPENROUTER_API_KEY` (or whichever provider you're using)
- `REGISTRY_USERNAME` - your Docker Hub username
- `REGISTRY_PASSWORD` - a Docker Hub [access token](https://hub.docker.com/settings/security)

3. Authenticate services

Make sure Docker is running locally, then:

```bash
modal setup          # Modal account for sandboxed agent execution
docker login         # Docker Hub for image pulls
```

4. Create a private Docker Hub repository

Go to [hub.docker.com](https://hub.docker.com) and create a new **private** repository (e.g., `anvil-images`).

> ⚠️ Public repos will not work. Anvil refuses to push task images to public repositories to prevent data leakage.

## Local Usage

1. Clone the repo you want to evaluate into `repos/`

```bash
git clone https://github.com/<org>/<repo_name> repos/<repo_name>
```

2. Convert the dataset (also warms the Xcode build cache)

```bash
anvil convert-dataset --dataset tasks/<repo_name>
```

3. Verify gold patches compile and pass unit tests

```bash
anvil run-evals --dataset datasets/<repo_name> --agent oracle
```

The oracle agent applies gold patches from `gold_patches.json` directly — all tests should pass if your harness is correct. Each dataset needs a `xcode_config.yaml` in `tasks/<repo_name>/` specifying the Xcode project, scheme, and build destination.

4. Publish Docker images (required for LLM agent runs via Modal)

```bash
anvil publish-images --dataset datasets/<repo_name>
```

The username and repo are read from `REGISTRY_USERNAME` and `REGISTRY_REPO` in `.env` (or pass `-u <username>` / `--repo <name>` to override).

5. Run evaluations

```bash
anvil run-evals \
  --dataset datasets/<repo_name> \
  --model openrouter/anthropic/claude-sonnet-4.5 \
  --agent mini-swe-agent \
  --n-attempts 4
```

By default each eval runs **unit tests** (`tests.swift`) and then **UI tests** (`uitests.swift`) when the task provides them. Pass **`--no-ui-tests`** to evaluate with unit tests only (skips copying and running UI tests). The run directory name gains a **`_unit-only`** suffix when `--no-ui-tests` is set (e.g. `mini-swe-agent_claude-sonnet-4.6_unit-only`), so full vs unit-only runs do not share the same folder.

Use `--n-attempts` to control how many runs per task (useful for pass@k metrics). Results are saved to `<dataset>/runs/<agent>_<model>/` (or `…_unit-only`).

> 💡 **Progress is saved automatically** to minimize costs. If you re-run the same command, completed tasks are skipped. Use `--no-continue` to start fresh.

### Options

| Flag                   | Default                 | Description                                         |
| ---------------------- | ----------------------- | --------------------------------------------------- |
| `--model`              | —                       | Model ID (required for agents, optional for oracle) |
| `--dataset`            | —                       | Dataset ID or path                                  |
| `--agent`              | mini-swe-agent          | Agent to use (`mini-swe-agent` or `oracle`)         |
| `--n-attempts`         | 1                       | Attempts per task (for pass@k)                      |
| `--no-ui-tests`        | false                   | Unit tests only; skip `uitests.swift` UI tests      |
| `--compile-only`       | false                   | Only check compilation, skip unit tests             |
| `--no-continue`        | false                   | Start fresh, ignore previous results                |
| `--max-parallel`       | 30                      | Concurrent agent runs                               |
| `--max-wait`           | auto                    | Minutes to wait for Modal rate limits               |

Docker Hub auth for Modal uses **`REGISTRY_USERNAME`** and **`REGISTRY_PASSWORD`** in `.env` (see Setup). Image names come from `instances.yaml` (set when you run `convert-dataset` / `publish-images`).

## Writing Tasks

Each task lives under `tasks/<repo_name>/task-N/` and requires three files:

**`problem.md`** — problem statement shown to the agent. Include:

- What to build and why
- Acceptance criteria
- A "Required API Surface" section listing exact type/method names the tests depend on (so the agent knows what names to expose)

**`solution.diff`** — the gold patch. Used by the oracle agent to verify the harness is correct before running LLM agents.

**`tests.swift`** — XCTest unit tests. The harness auto-routes based on imports:

- `import <SPMPackage>` only → copied into the SPM package test target (`test_files_dest` in `xcode_config.yaml`)
- `@testable import <AppModule>` → injected into the app test target
- Use `uitests.swift` instead for XCUIApplication UI tests (auto-routed to the UI test target)

**`metadata.yaml`** (dataset-level, not per-task) — maps each task to its base commit SHA in the source repo:

```yaml
base_commits:
  task-1: <sha>
  task-2: <sha>
```

**`xcode_config.yaml`** (dataset-level) — configures the Xcode build. At minimum:

```yaml
project: <RepoName>/<RepoName>.xcodeproj
scheme: <SchemeName>
test_package_path: <RepoName>/Packages/<PackageName>
test_files_dest: Tests/<TargetName>
test_scheme: <PackageScheme>
test_destination: "platform=iOS Simulator,name=iPhone 16,OS=latest"
```

For app-level tests (`@testable import`), add `app_test_target` and `app_test_files_dest`. Only set `app_test_module` if the Swift module name differs from `scheme`.

After writing tasks, run the oracle to verify all gold patches pass before running LLM agents:

```bash
anvil run-evals --dataset datasets/<repo_name> --agent oracle --no-continue
```

## GitHub Actions

GitHub Actions workflows are included in the repo under `.github/workflows/`. The full eval pipeline can be run directly on GitHub.

1. Configure the following repository secrets under **Settings → Secrets and variables → Actions**:
   - `OPENROUTER_API_KEY`
   - `MODAL_TOKEN_ID`
   - `MODAL_TOKEN_SECRET`
   - `REGISTRY_USERNAME`
   - `REGISTRY_PASSWORD`

2. Go to **Actions → Anvil Eval**, click **Run workflow**, and pick a dataset, model, agent, and number of attempts from the dropdowns. See an [example run](https://github.com/AfterQuery/swift-anvil/actions/workflows/eval.yml).

Results are committed back to the repo under `gha_runs/` and are also available as workflow artifacts.
