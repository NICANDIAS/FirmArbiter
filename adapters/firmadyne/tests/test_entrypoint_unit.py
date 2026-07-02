"""Unit tests for FIRMADYNE adapter entrypoint v0.1.0.

These tests run without Docker, without firmware, and without PostgreSQL.
They verify contract structure, event emission and outcome semantics.
"""
import inspect
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from entrypoint import (
    AdapterOperationalError,
    ContractError,
    EventWriter,
    HeartbeatWorker,
    map_contract_path,
    require_object,
    utc_now,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_writer(tmp_path: Path) -> tuple[EventWriter, Path]:
    events_path = tmp_path / "events.jsonl"
    writer = EventWriter(events_path, run_id="test-001", adapter_id="firmadyne")
    return writer, events_path


def read_events(events_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# EventWriter tests
# ---------------------------------------------------------------------------

def test_event_writer_emits_required_fields(tmp_path):
    writer, events_path = make_writer(tmp_path)
    writer.emit("adapter_started", state="starting")
    writer.close()

    events = read_events(events_path)
    assert len(events) == 1
    e = events[0]
    assert e["event"] == "adapter_started"
    assert e["schema_version"] == "1.0"
    assert e["contract_version"] == "1.0"
    assert e["run_id"] == "test-001"
    assert e["adapter_id"] == "firmadyne"
    assert e["sequence"] == 1
    assert "timestamp" in e
    assert e["state"] == "starting"
    print("PASS test_event_writer_emits_required_fields")


def test_event_writer_sequence_increments(tmp_path):
    writer, events_path = make_writer(tmp_path)
    for i in range(5):
        writer.emit("heartbeat", state="running")
    writer.close()

    events = read_events(events_path)
    sequences = [e["sequence"] for e in events]
    assert sequences == [1, 2, 3, 4, 5]
    print("PASS test_event_writer_sequence_increments")


def test_event_writer_rejects_emit_after_close(tmp_path):
    writer, events_path = make_writer(tmp_path)
    writer.emit("adapter_started", state="starting")
    writer.close()
    try:
        writer.emit("heartbeat", state="running")
        assert False, "Expected RuntimeError"
    except RuntimeError:
        pass
    print("PASS test_event_writer_rejects_emit_after_close")


def test_event_writer_thread_safe(tmp_path):
    writer, events_path = make_writer(tmp_path)
    threads = []
    for _ in range(10):
        t = threading.Thread(
            target=lambda: writer.emit("heartbeat", state="running")
        )
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    writer.close()

    events = read_events(events_path)
    assert len(events) == 10
    sequences = sorted(e["sequence"] for e in events)
    assert sequences == list(range(1, 11))
    print("PASS test_event_writer_thread_safe")


# ---------------------------------------------------------------------------
# HeartbeatWorker tests
# ---------------------------------------------------------------------------

def test_heartbeat_emits_events(tmp_path):
    writer, events_path = make_writer(tmp_path)
    state = {"value": "running"}
    hb = HeartbeatWorker(
        event_writer=writer,
        interval_seconds=0.1,
        state_getter=lambda: state["value"],
    )
    hb.start()
    time.sleep(0.45)
    hb.stop()
    writer.close()

    events = read_events(events_path)
    heartbeats = [e for e in events if e["event"] == "heartbeat"]
    assert len(heartbeats) >= 3, f"Expected >=3 heartbeats, got {len(heartbeats)}"
    assert all(e["state"] == "running" for e in heartbeats)
    print(f"PASS test_heartbeat_emits_events ({len(heartbeats)} heartbeats)")


def test_heartbeat_reflects_state_change(tmp_path):
    writer, events_path = make_writer(tmp_path)
    state = {"value": "starting"}
    hb = HeartbeatWorker(
        event_writer=writer,
        interval_seconds=0.1,
        state_getter=lambda: state["value"],
    )
    hb.start()
    time.sleep(0.15)
    state["value"] = "running"
    time.sleep(0.15)
    hb.stop()
    writer.close()

    events = read_events(events_path)
    states = [e["state"] for e in events if e["event"] == "heartbeat"]
    assert "starting" in states
    assert "running" in states
    print("PASS test_heartbeat_reflects_state_change")


# ---------------------------------------------------------------------------
# Contract utility tests
# ---------------------------------------------------------------------------

def test_require_object_passes_dict():
    result = require_object({"key": "value"}, "test")
    assert result == {"key": "value"}
    print("PASS test_require_object_passes_dict")


def test_require_object_rejects_non_dict():
    try:
        require_object("not a dict", "test")
        assert False, "Expected ContractError"
    except ContractError:
        pass
    print("PASS test_require_object_rejects_non_dict")


def test_utc_now_format():
    ts = utc_now()
    assert ts.endswith("Z"), f"Expected Z suffix: {ts}"
    assert "T" in ts
    print(f"PASS test_utc_now_format ({ts})")


def test_map_contract_path_no_root(tmp_path, monkeypatch=None):
    import os
    os.environ.pop("VERITAS_CONTRACT_ROOT", None)
    p = map_contract_path("/veritas/events/events.jsonl")
    assert str(p) == "/veritas/events/events.jsonl"
    print("PASS test_map_contract_path_no_root")


# ---------------------------------------------------------------------------
# AdapterOperationalError tests
# ---------------------------------------------------------------------------

def test_adapter_operational_error_fields():
    exc = AdapterOperationalError(
        "TEST_CODE", "test_phase", "test message", recoverable=True
    )
    assert exc.code == "TEST_CODE"
    assert exc.phase == "test_phase"
    assert str(exc) == "test message"
    assert exc.recoverable is True
    print("PASS test_adapter_operational_error_fields")


def test_adapter_operational_error_default_not_recoverable():
    exc = AdapterOperationalError("CODE", "phase", "msg")
    assert exc.recoverable is False
    print("PASS test_adapter_operational_error_default_not_recoverable")


# ---------------------------------------------------------------------------
# Stage outcome event structure tests
# ---------------------------------------------------------------------------

def test_stage_completed_failed_structure(tmp_path):
    """Verify a failed unpack stage_completed event has required fields."""
    writer, events_path = make_writer(tmp_path)
    writer.emit(
        "stage_completed",
        stage="unpack",
        stage_outcome="failed",
        duration_seconds=1.13,
        detail="Extractor produced no image ID in database.",
    )
    writer.close()

    events = read_events(events_path)
    e = events[0]
    assert e["event"] == "stage_completed"
    assert e["stage"] == "unpack"
    assert e["stage_outcome"] == "failed"
    assert "duration_seconds" in e
    assert "detail" in e
    print("PASS test_stage_completed_failed_structure")


def test_adapter_stopped_failed_outcome(tmp_path):
    """Verify adapter_stopped carries outcome field."""
    writer, events_path = make_writer(tmp_path)
    writer.emit("adapter_stopped", outcome="failed")
    writer.close()

    events = read_events(events_path)
    assert events[0]["outcome"] == "failed"
    print("PASS test_adapter_stopped_failed_outcome")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    tests = [
        test_event_writer_emits_required_fields,
        test_event_writer_sequence_increments,
        test_event_writer_rejects_emit_after_close,
        test_event_writer_thread_safe,
        test_heartbeat_emits_events,
        test_heartbeat_reflects_state_change,
        test_require_object_passes_dict,
        test_require_object_rejects_non_dict,
        test_utc_now_format,
        test_map_contract_path_no_root,
        test_adapter_operational_error_fields,
        test_adapter_operational_error_default_not_recoverable,
        test_stage_completed_failed_structure,
        test_adapter_stopped_failed_outcome,
    ]

    passed = 0
    failed = 0

    for test in tests:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                sig = inspect.signature(test)
                if sig.parameters:
                    test(Path(tmp))
                else:
                    test()
                passed += 1
            except Exception as exc:
                print(f"FAIL {test.__name__}: {exc}")
                failed += 1

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
