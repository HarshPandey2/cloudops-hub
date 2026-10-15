import pytest

from app.services.job_handlers import (
    JobPayloadError,
    run_job_handler,
    validate_job_input,
)


def test_registered_data_processing_job_returns_summary():
    assert run_job_handler("data.process", {"values": [1, 2.5, 3]}) == {
        "count": 3,
        "sum": 6.5,
        "minimum": 1.0,
        "maximum": 3.0,
        "average": pytest.approx(6.5 / 3),
    }


def test_job_registry_rejects_unknown_types_and_invalid_payloads():
    with pytest.raises(JobPayloadError, match="Unsupported"):
        validate_job_input("shell.run", {"command": "whoami"})
    with pytest.raises(JobPayloadError, match="Invalid input"):
        validate_job_input("simulation.delay", {"seconds": 31})
    with pytest.raises(JobPayloadError, match="Invalid input"):
        validate_job_input("data.process", {"values": [float("inf")]})


def test_health_job_returns_bounded_runtime_metadata():
    result = run_job_handler("system.health", {})
    assert result["status"] == "ok"
    assert "python_version" in result
