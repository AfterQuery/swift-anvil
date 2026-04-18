Animal Crossing Helper: https://github.com/Dimillian/ACHNBrowserUI

## Tasks

1. Catch Now: https://github.com/Dimillian/ACHNBrowserUI/pull/263

- Type: Feature
- Patch: curl -L https://github.com/Dimillian/ACHNBrowserUI/pull/263.diff -o solution.diff
- Base Commit: 3eba9512b0fb430d2507e27df3f8311d3bd67706

2. Adjust Grid View: https://github.com/Dimillian/ACHNBrowserUI/pull/191

- Type: Fix
- Patch: curl -L https://github.com/Dimillian/ACHNBrowserUI/pull/191.diff -o solution.diff
- Base Commit: 31b3185f7435e9f1c208ad7c0c726a54652ca791

3. Add Partial Like: https://github.com/Dimillian/ACHNBrowserUI/pull/338

- Type: Feature
- Patch: curl -L https://github.com/Dimillian/ACHNBrowserUI/pull/338.diff -o solution.diff
- Base Commit: 3d11674846dd9ad905de616782134b0a76a4e148

4. Add Sorting Villagers: https://github.com/Dimillian/ACHNBrowserUI/pull/190

- Type: Feature
- Patch: curl -L https://github.com/Dimillian/ACHNBrowserUI/pull/190.diff -o solution.diff
- Base Commit: 89ac53bfe6d0769411f4005060e8974fa8fd35d4

5. Add Custom Chores and To-Dos: https://github.com/Dimillian/ACHNBrowserUI/pull/210

- Type: Feature
- Patch: curl -L https://github.com/Dimillian/ACHNBrowserUI/pull/210.diff -o solution.diff
- Base Commit: 848a1589eb08f89f6badfde10d3b10ea592157a5

6. ACHN Dashboard: https://github.com/Dimillian/ACHNBrowserUI/pull/22

- Type: Feature
- Patch: curl -L https://github.com/Dimillian/ACHNBrowserUI/pull/22.diff -o solution.diff
- Base Commit: 80994d6ffa112b654312aa4922897de92e986ba3

7. Turnip Exchange Listing: https://github.com/Dimillian/ACHNBrowserUI/pull/15

- Type: Feature
- Patch: curl -L https://github.com/Dimillian/ACHNBrowserUI/pull/15.diff -o solution.diff
- Base Commit: 87dace0a0d0a120fd81d651e36bd45c4cc95470b

8. Turnip Prices Min Max Average: https://github.com/Dimillian/ACHNBrowserUI/pull/175

- Type: Feature
- Patch: curl -L https://github.com/Dimillian/ACHNBrowserUI/pull/175.diff -o solution.diff
- Base Commit: ceba2fddea5304b2b248e5de72568d3afdfbb97a

9. Today Villager Visits: https://github.com/Dimillian/ACHNBrowserUI/pull/240

- Type: Feature
- Patch: curl -L https://github.com/Dimillian/ACHNBrowserUI/pull/240.diff -o solution.diff
- Base Commit: d01e4353bd393e5fefcdac8910d9adc1d9ce7892

10. Add Creator / Custom Design Items: https://github.com/Dimillian/ACHNBrowserUI/pull/189

- Type: Feature
- Patch: curl -L https://github.com/Dimillian/ACHNBrowserUI/pull/189.diff -o solution.diff
- Base Commit: e4c80e95cc9ac5a0870c7e749d1c7c9b219bb360

## Commands

```bash
source .venv/bin/activate
```

0. Create task directories from GitHub PRs (skip if task dirs already exist)

Add PR URLs (one per line) to `src/anvil/commands/github_prs/ACHNBrowserUI.txt`, then run:

```bash
anvil create-tasks ACHNBrowserUI
```

1. Convert dataset

```bash
anvil convert-dataset --dataset tasks/ACHNBrowserUI
```

2. Verify gold patches

```bash
anvil run-evals --dataset datasets/ACHNBrowserUI --agent oracle --no-continue
```

3. Publish Docker images

```bash
anvil publish-images --dataset datasets/ACHNBrowserUI
```

4. Run against models

By default, `run-evals` runs **unit tests** (`tests.swift`) and **UI tests** (`uitests.swift`) when a task has both. Append **`--no-ui-tests`** to any command below to evaluate **unit tests only** (skips UI tests). With `--no-ui-tests`, results go under `runs/<agent>_<model>_unit-only/` instead of `runs/<agent>_<model>/`.

```bash
anvil run-evals --dataset datasets/ACHNBrowserUI --agent mini-swe-agent --model openrouter/anthropic/claude-opus-4.6 --n-attempts 4 --no-continue

anvil run-evals --dataset datasets/ACHNBrowserUI --agent mini-swe-agent --model openrouter/anthropic/claude-sonnet-4.5 --n-attempts 4 --no-continue

anvil run-evals --dataset datasets/ACHNBrowserUI --agent mini-swe-agent --model openrouter/anthropic/claude-sonnet-4.6 --n-attempts 4 --no-continue

anvil run-evals --dataset datasets/ACHNBrowserUI --agent mini-swe-agent --model openrouter/openai/gpt-5.4 --n-attempts 4 --no-continue

anvil run-evals --dataset datasets/ACHNBrowserUI --agent mini-swe-agent --model openrouter/openai/gpt-5.2 --n-attempts 4 --no-continue

anvil run-evals --dataset datasets/ACHNBrowserUI --agent mini-swe-agent --model openrouter/openai/gpt-5.3-codex --n-attempts 4 --no-continue

anvil run-evals --dataset datasets/ACHNBrowserUI --agent mini-swe-agent --model openrouter/openai/gpt-5.2-codex --n-attempts 4 --no-continue

anvil run-evals --dataset datasets/ACHNBrowserUI --agent mini-swe-agent --model openrouter/google/gemini-3.1-pro-preview --n-attempts 4 --no-continue

anvil run-evals --dataset datasets/ACHNBrowserUI --agent mini-swe-agent --model openrouter/google/gemini-3-pro-preview --n-attempts 4 --no-continue

anvil run-evals --dataset datasets/ACHNBrowserUI --agent mini-swe-agent --model openrouter/qwen/qwen3-coder-next --n-attempts 4 --no-continue

anvil run-evals --dataset datasets/ACHNBrowserUI --agent mini-swe-agent --model openrouter/deepseek/deepseek-v3.2 --n-attempts 4 --no-continue
```
