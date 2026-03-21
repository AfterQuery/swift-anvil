from anvil.evals.xcode_parser import merge_test_results, parse_build_result, parse_xcodebuild_output
from anvil.evals.constants import TEST_STATUS_FAILED, TEST_STATUS_PASSED


def test_parse_passed_test_case():
    stdout = "Test Case '-[MyTests testFoo]' passed (0.001 seconds)."
    result = parse_xcodebuild_output(stdout, "")
    tests = result["tests"]
    assert len(tests) == 1
    assert tests[0]["name"] == "testFoo"
    assert tests[0]["class_name"] == "MyTests"
    assert tests[0]["status"] == TEST_STATUS_PASSED


def test_parse_failed_test_case():
    stdout = "Test Case '-[MyTests testBar]' failed (0.010 seconds)."
    result = parse_xcodebuild_output(stdout, "")
    assert result["tests"][0]["status"] == TEST_STATUS_FAILED


def test_parse_multiple_tests():
    stdout = (
        "Test Case '-[Suite testA]' passed (0.001 seconds).\n"
        "Test Case '-[Suite testB]' failed (0.002 seconds).\n"
    )
    result = parse_xcodebuild_output(stdout, "")
    assert len(result["tests"]) == 2


def test_parse_test_in_stderr():
    stderr = "Test Case '-[MyTests testBaz]' passed (0.001 seconds)."
    result = parse_xcodebuild_output("", stderr)
    assert result["tests"][0]["status"] == TEST_STATUS_PASSED


def test_parse_empty():
    result = parse_xcodebuild_output("", "")
    assert result["tests"] == []


def test_parse_build_result_success():
    result = parse_build_result(0, "", "")
    tests = result["tests"]
    assert len(tests) == 1
    assert tests[0]["status"] == TEST_STATUS_PASSED


def test_parse_build_result_failure_includes_error():
    stderr = "Foo.swift:10:5: error: use of undeclared identifier 'bar'"
    result = parse_build_result(1, "", stderr)
    tests = result["tests"]
    assert tests[0]["status"] == TEST_STATUS_FAILED
    assert "undeclared identifier" in tests[0]["message"]


def test_parse_build_result_failure_no_errors():
    result = parse_build_result(1, "", "")
    assert result["tests"][0]["status"] == TEST_STATUS_FAILED
    assert result["tests"][0]["message"] == "Build failed"


def test_merge_two_results():
    a = {"tests": [{"name": "t1", "status": TEST_STATUS_PASSED}]}
    b = {"tests": [{"name": "t2", "status": TEST_STATUS_FAILED}]}
    merged = merge_test_results(a, b)
    assert len(merged["tests"]) == 2


def test_merge_with_none():
    a = {"tests": [{"name": "t1", "status": TEST_STATUS_PASSED}]}
    merged = merge_test_results(a, None)
    assert len(merged["tests"]) == 1


def test_merge_empty():
    merged = merge_test_results({}, {})
    assert merged["tests"] == []
