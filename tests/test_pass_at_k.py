from anvil.evals.pass_at_k import estimate_pass_at_k, compute_pass_at_k_summary


def test_all_correct():
    assert estimate_pass_at_k(5, 5, 1) == 1.0


def test_none_correct():
    assert estimate_pass_at_k(5, 0, 1) == 0.0


def test_n_less_than_k_some_correct():
    # n < k with at least one success → always solvable
    assert estimate_pass_at_k(2, 1, 5) == 1.0


def test_n_less_than_k_none_correct():
    assert estimate_pass_at_k(2, 0, 5) == 0.0


def test_pass_at_1_partial():
    # 1 correct out of 3: pass@1 = 1 - C(2,1)/C(3,1) = 1/3
    result = estimate_pass_at_k(3, 1, 1)
    assert abs(result - 1 / 3) < 1e-9


def test_pass_at_k_all_correct():
    # n == c → 1.0
    result = estimate_pass_at_k(4, 4, 2)
    assert result == 1.0


def test_compute_summary_task_count():
    results = {"task-1": [True, False, True], "task-2": [False, False]}
    summary = compute_pass_at_k_summary(
        results, "gpt-4o", "ACHNBrowserUI", "oracle", k=2, duration_seconds=60.0
    )
    assert summary.n_tasks == 2
    assert summary.total_runs == 5
    assert summary.k == 2


def test_compute_summary_aggregate_pass_at_1():
    # task-1: 2/3 correct → pass@1 = 1 - C(1,1)/C(3,1) = 2/3
    # task-2: 0/2 correct → pass@1 = 0
    results = {"task-1": [True, False, True], "task-2": [False, False]}
    summary = compute_pass_at_k_summary(
        results, "model", "ds", "agent", k=1, duration_seconds=0.0
    )
    expected = (2 / 3 + 0.0) / 2
    assert abs(summary.aggregate_pass_at_1 - expected) < 1e-9


def test_compute_summary_empty():
    summary = compute_pass_at_k_summary({}, "m", "d", "a", k=1, duration_seconds=0.0)
    assert summary.n_tasks == 0
    assert summary.aggregate_pass_at_1 == 0.0
