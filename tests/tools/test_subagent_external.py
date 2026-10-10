"""External (non-delegate_task) subagent observer — tools/subagent_external.py.

Contract tests: the unix-socket command protocol, registry merge semantics
(status normalization, staleness pruning, bounded FIFO), and the opt-in gate on
``delegation.external_subagent_registry``.
"""

import json
import os
import socket
import threading
import time
from unittest.mock import patch as mock_patch

import pytest

from tools import subagent_external


def _send(sock_path, *commands):
    """Connect to the observer socket and send newline-delimited JSON commands."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(sock_path))
        for cmd in commands:
            s.sendall(json.dumps(cmd).encode() + b"\n")
        time.sleep(0.15)  # allow the server thread to process
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _clean_state():
    subagent_external.clear_external_records()
    yield
    subagent_external.stop_observer()
    subagent_external.clear_external_records()


def test_register_update_unregister_roundtrip(tmp_path):
    sock = subagent_external.start_observer(tmp_path / "obs.sock")
    assert sock is not None and sock.exists()

    started = time.time()
    _send(sock, {
        "op": "register", "subagent_id": "ext-1", "goal": "research el nino",
        "model": "qwen3.8-flash-next", "server_ip": "192.168.1.227",
        "started_at": started, "agent_id": "builder-224-1", "channel_id": "default",
    })
    recs = subagent_external.external_records_snapshot()
    assert len(recs) == 1
    rec = recs[0]
    assert rec["subagent_id"] == "ext-1"
    assert rec["kind"] == "external"
    assert rec["status"] == "running"
    assert rec["server_ip"] == "192.168.1.227"
    assert rec["goal"] == "research el nino"

    _send(sock, {"op": "update", "subagent_id": "ext-1", "last_tool": "Bash", "tool_count": 7})
    rec = subagent_external.external_records_snapshot()[0]
    assert rec["last_tool"] == "Bash"
    assert rec["tool_count"] == 7

    _send(sock, {
        "op": "unregister", "subagent_id": "ext-1", "status": "completed",
        "duration_seconds": 12.5, "input_tokens": 40000, "output_tokens": 100,
        "num_turns": 3,
    })
    assert subagent_external.external_records_snapshot() == []


def test_unknown_and_malformed_commands_are_ignored(tmp_path):
    sock = subagent_external.start_observer(tmp_path / "obs.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(sock))
        s.sendall(b"not json at all\n")
        s.sendall(json.dumps({"op": "frobnicate", "subagent_id": "x"}).encode() + b"\n")
        s.sendall(json.dumps({"op": "update", "subagent_id": "never-registered"}).encode() + b"\n")
        s.sendall(json.dumps({"op": "register"}).encode() + b"\n")  # no subagent_id
        time.sleep(0.15)
    finally:
        s.close()
    assert subagent_external.external_records_snapshot() == []


def test_merge_normalizes_killed_and_prunes_stale():
    now = time.time()
    with subagent_external._external_lock:
        subagent_external._external_records["live-1"] = {
            "subagent_id": "live-1", "goal": "g", "status": "killed",
            "kind": "external", "_updated_at": now,
        }
        subagent_external._external_records["stale-1"] = {
            "subagent_id": "stale-1", "goal": "g", "status": "running",
            "kind": "external", "_updated_at": now - subagent_external._EXTERNAL_STALE_SECONDS - 5,
        }
    merged = subagent_external.merge_external_into([{"subagent_id": "native-1"}])
    ids = [r["subagent_id"] for r in merged]
    assert "native-1" in ids
    assert "live-1" in ids
    assert "stale-1" not in ids
    live = next(r for r in merged if r["subagent_id"] == "live-1")
    assert live["status"] == "interrupted"  # TUI SubagentStatus vocabulary


def test_fifo_bound_evicts_oldest():
    now = time.time()
    with subagent_external._external_lock:
        for i in range(subagent_external._MAX_EXTERNAL_RECORDS):
            subagent_external._external_records[f"old-{i}"] = {
                "subagent_id": f"old-{i}", "status": "running", "_updated_at": now + i,
            }
    ok = subagent_external._apply_register({"subagent_id": "new-1"})
    assert ok
    with subagent_external._external_lock:
        assert "new-1" in subagent_external._external_records
        assert len(subagent_external._external_records) == subagent_external._MAX_EXTERNAL_RECORDS
        assert "old-0" not in subagent_external._external_records  # oldest evicted


def test_is_enabled_reads_config(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/tmp/unused-home-for-this-test")
    with mock_patch("hermes_cli.config.load_config_readonly",
                    return_value={"delegation": {"external_subagent_registry": True}}):
        assert subagent_external.is_enabled() is True
    with mock_patch("hermes_cli.config.load_config_readonly",
                    return_value={"delegation": {"external_subagent_registry": False}}):
        assert subagent_external.is_enabled() is False
    with mock_patch("hermes_cli.config.load_config_readonly", return_value={}):
        assert subagent_external.is_enabled() is False
    with mock_patch("hermes_cli.config.load_config_readonly", side_effect=RuntimeError("boom")):
        assert subagent_external.is_enabled() is False  # fail-open


def test_list_active_subagents_merges_when_enabled(monkeypatch):
    from tools import delegate_tool_registry as reg
    now = time.time()
    with reg._active_subagents_lock:
        reg._active_subagents["sa-native"] = {
            "subagent_id": "sa-native", "goal": "native goal", "status": "running",
            "started_at": now, "agent": object(),
        }
    with subagent_external._external_lock:
        subagent_external._external_records["ext-merged"] = {
            "subagent_id": "ext-merged", "goal": "external goal", "status": "running",
            "started_at": now, "kind": "external", "_updated_at": now,
        }
    try:
        with mock_patch("tools.subagent_external.is_enabled", return_value=True):
            out = reg.list_active_subagents()
        ids = {r["subagent_id"] for r in out}
        assert ids == {"sa-native", "ext-merged"}
        ext = next(r for r in out if r["subagent_id"] == "ext-merged")
        assert ext["kind"] == "external"
        # Private native keys never leak into the snapshot.
        assert "agent" not in next(r for r in out if r["subagent_id"] == "sa-native")

        with mock_patch("tools.subagent_external.is_enabled", return_value=False):
            out_off = reg.list_active_subagents()
        assert {r["subagent_id"] for r in out_off} == {"sa-native"}
    finally:
        with reg._active_subagents_lock:
            reg._active_subagents.pop("sa-native", None)
            reg._active_subagents.pop("ext-merged", None)


def test_concurrent_connections_do_not_interleave(tmp_path):
    """Two launchers registering different children at once both land intact."""
    sock = subagent_external.start_observer(tmp_path / "obs.sock")
    errors = []

    def worker(n):
        try:
            _send(sock, {
                "op": "register", "subagent_id": f"conc-{n}", "goal": f"g{n}",
                "model": "m", "started_at": time.time(),
            })
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    ids = {r["subagent_id"] for r in subagent_external.external_records_snapshot()}
    assert ids == {f"conc-{i}" for i in range(4)}
