import importlib.util
from pathlib import Path

import pytest


def lab_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "process_lab.py"
    spec = importlib.util.spec_from_file_location("process_lab_clock", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_clock_rollback_does_not_break_monotonic_peak():
    # The DB's wall-clock intervals may go backwards. Independent monotonic evidence
    # must still reconstruct a serialized execution; it must not clamp wall values.
    rows = [
        {"operation_id": "one", "started_at": 100, "completed_at": 91},
        {"operation_id": "two", "started_at": 92, "completed_at": 93},
    ]
    journal = [
        {"operation_id": "one", "event": "started", "time_ns": 10, "seq": 1},
        {"operation_id": "one", "event": "terminal", "time_ns": 20, "seq": 2},
        {"operation_id": "two", "event": "started", "time_ns": 21, "seq": 3},
        {"operation_id": "two", "event": "terminal", "time_ns": 30, "seq": 4},
    ]
    assert hasattr(lab_module(), "summarize_monotonic"), "monotonic observer is missing"
    assert lab_module().summarize_monotonic(journal, {row["operation_id"] for row in rows}, 1) == 1


def test_monotonic_peak_cannot_hide_overlap():
    journal = [
        {"operation_id": "one", "event": "started", "time_ns": 10, "seq": 1},
        {"operation_id": "two", "event": "started", "time_ns": 11, "seq": 2},
        {"operation_id": "one", "event": "terminal", "time_ns": 20, "seq": 3},
        {"operation_id": "two", "event": "terminal", "time_ns": 30, "seq": 4},
    ]
    with pytest.raises(AssertionError):
        lab_module().summarize_monotonic(journal, {"one", "two"}, 1)


def test_monotonic_journal_rejects_missing_or_duplicate_terminal():
    start = {"operation_id": "one", "event": "started", "time_ns": 10, "seq": 1}
    terminal = {"operation_id": "one", "event": "terminal", "time_ns": 20, "seq": 2}
    for journal in [[start], [start, terminal, dict(terminal, seq=3)]]:
        with pytest.raises(AssertionError):
            lab_module().summarize_monotonic(journal, {"one"}, 1)


async def test_tools_database_guard_precedes_any_initialization(monkeypatch):
    monkeypatch.setenv(
        "TOOLGATE_TEST_DATABASE_URL", "postgresql+psycopg://example:example@127.0.0.1/toolgate_test"
    )
    monkeypatch.setenv(
        "MOCK_TOOLS_TEST_DATABASE_URL", "postgresql+psycopg://example:example@127.0.0.1/toolgate"
    )
    with pytest.raises(ValueError, match="Tools test database name"):
        async with lab_module().ProcessLab():
            pytest.fail("non-test tools database was accepted")
