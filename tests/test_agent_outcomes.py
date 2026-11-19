from packages.agent_contracts.outcomes import is_retryable_outcome


def test_blocked_and_succeeded_outcomes_are_never_retried():
    assert is_retryable_outcome({"status": "blocked", "error_code": "X_API_UNAVAILABLE"}) is False
    assert is_retryable_outcome({"status": "succeeded", "error_code": None}) is False


def test_transient_failures_and_idempotent_mutations_are_retryable():
    assert is_retryable_outcome({
        "status": "failed",
        "error_code": "NETWORK_STACK_API_UNAVAILABLE",
        "side_effects": False,
    })
    assert is_retryable_outcome({
        "status": "failed",
        "error_code": "CAPI_PIVOT_KUBECTL_READ_FAILED",
        "side_effects": True,
    })


def test_invalid_spec_failure_is_not_retried():
    assert is_retryable_outcome({
        "status": "failed",
        "error_code": "CONTROL_STACK_SPEC_INVALID",
        "side_effects": False,
    }) is False
