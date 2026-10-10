"""External (non-delegate_task) subagent observer.

Headless subagents launched OUTSIDE ``delegate_task`` — most notably Claude Code
(``claude -p``) runs dispatched by agent-team profiles via plain terminal — leave no
trace in the live-subagent registry, so the ctrl+t overlay and ``delegation.status``
RPC never see them. This module closes that gap:

* An opt-in unix-socket observer (one per gateway process) accepts small
  newline-delimited JSON commands from an external launcher
  (``scripts/dispatch-claude-subagent.py`` in the agent-team repo):
  ``register`` / ``update`` / ``unregister``.
* Accepted records live in a bounded dict and are merged into
  ``list_active_subagents()`` (the feed for the TUI agents overlay and the
  ``delegation.status`` RPC), tagged ``kind="external"``.
* Records carry no agent object: steering/interrupt/tail helpers already fail
  closed on them (they look up ``record["agent"]``); ownership checks treat them
  as owned by the session that registered them (``owner_session_id`` is stamped by
  the gateway at accept time).

Fail-open everywhere: a missing socket, a malformed command, or a dead peer must
never disturb the owning gateway process. Enabled via
``delegation.external_subagent_registry: true`` in config.yaml (default off).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("tools.subagent_external")

_CONFIG_KEY = ("delegation", "external_subagent_registry")
# External records are refreshed by the launcher while the child runs; anything
# older than this has lost its launcher and is pruned on the next merge.
_EXTERNAL_STALE_SECONDS = 600.0
_MAX_EXTERNAL_RECORDS = 200
_MAX_LINE_BYTES = 8192


def is_enabled() -> bool:
    """True when the operator opted in via ``delegation.external_subagent_registry``."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
    except Exception:
        return False
    node: Any = cfg
    for key in _CONFIG_KEY:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return bool(node)


def socket_path(hermes_home: Optional[str] = None) -> Path:
    """Per-profile socket location (profile-aware: keys off HERMES_HOME)."""
    base = hermes_home
    if not base:
        try:
            from hermes_constants import get_hermes_home
            base = str(get_hermes_home())
        except Exception:
            base = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(base) / "cache" / "subagent_obs.sock"


_external_lock = threading.Lock()
_external_records: Dict[str, Dict[str, Any]] = {}
_server_state: Dict[str, Any] = {"thread": None, "sock": None, "path": None}


def _stamp_owner(record: Dict[str, Any]) -> None:
    """Stamp the accepting gateway session onto the record so session-scoped views
    (TUI roster) attribute it to the viewer, mirroring how delegate_task children
    freeze ``owner_session_id`` at spawn."""
    try:
        from gateway.session_context import get_session_env
        sid = get_session_env("HERMES_UI_SESSION_ID", "") or ""
    except Exception:
        sid = ""
    if sid:
        record.setdefault("owner_session_id", sid)


def _apply_register(payload: Dict[str, Any]) -> bool:
    sid = str(payload.get("subagent_id") or "").strip()
    if not sid:
        return False
    with _external_lock:
        if len(_external_records) >= _MAX_EXTERNAL_RECORDS and sid not in _external_records:
            # Bounded FIFO: drop the oldest record rather than grow unbounded.
            oldest = min(_external_records, key=lambda k: _external_records[k].get("_updated_at") or 0)
            _external_records.pop(oldest, None)
        rec: Dict[str, Any] = {
            "subagent_id": sid,
            "parent_id": None,
            "depth": 0,
            "goal": str(payload.get("goal") or ""),
            "model": payload.get("model"),
            "status": "running",
            "started_at": float(payload.get("started_at") or time.time()),
            "tool_count": 0,
            "last_tool": None,
            "accepting_steer": False,
            "kind": "external",
            "server_ip": payload.get("server_ip"),
            "run_id": payload.get("run_id"),
            "agent_id": payload.get("agent_id"),
            "channel_id": payload.get("channel_id"),
            "_updated_at": time.time(),
        }
        _stamp_owner(rec)
        _external_records[sid] = rec
    return True


def _apply_update(payload: Dict[str, Any]) -> bool:
    sid = str(payload.get("subagent_id") or "").strip()
    if not sid:
        return False
    allowed = {
        "status": ("queued", "running", "completed", "failed", "error", "timeout", "interrupted", "killed"),
        "last_tool": None, "tool_count": int, "goal": str, "model": str,
        "summary": str, "duration_seconds": float, "input_tokens": int,
        "output_tokens": int, "num_turns": int, "cost_usd": float,
    }
    with _external_lock:
        rec = _external_records.get(sid)
        if rec is None:
            return False
        for key, typ in allowed.items():
            if key not in payload:
                continue
            val = payload[key]
            if typ is int and isinstance(val, bool):
                continue
            if typ is not None and not isinstance(val, typ):
                try:
                    val = typ(val)
                except (TypeError, ValueError):
                    continue
            rec[key] = val
        rec["_updated_at"] = time.time()
    return True


def _apply_unregister(payload: Dict[str, Any]) -> bool:
    sid = str(payload.get("subagent_id") or "").strip()
    if not sid:
        return False
    with _external_lock:
        rec = _external_records.pop(sid, None)
    if rec is None:
        return False
    for key in ("status", "summary", "duration_seconds", "input_tokens", "output_tokens",
                "num_turns", "cost_usd", "last_tool", "tool_count"):
        if payload.get(key) is not None:
            rec[key] = payload[key]
    return True


def _handle_command(line: bytes) -> None:
    try:
        cmd = json.loads(line.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return
    if not isinstance(cmd, dict):
        return
    op = cmd.get("op")
    if op == "register":
        _apply_register(cmd)
    elif op == "update":
        _apply_update(cmd)
    elif op == "unregister":
        _apply_unregister(cmd)


def _serve_conn(conn: socket.socket) -> None:
    buf = b""
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if len(line) > _MAX_LINE_BYTES:
                    continue
                _handle_command(line)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def start_observer(path: Optional[Path] = None) -> Optional[Path]:
    """Start the unix-socket observer in this process. Idempotent; returns the bound
    path, or None when it could not start (fail-open)."""
    with _external_lock:
        if _server_state["thread"] is not None:
            return _server_state["path"]
    path = path or socket_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        sock.listen(16)
        sock.settimeout(0.5)
        thread = threading.Thread(target=_serve_loop, args=(sock,), daemon=True,
                                  name="subagent-obs")
        thread.start()
        with _external_lock:
            _server_state.update({"thread": thread, "sock": sock, "path": path})
        logger.info("External subagent observer listening on %s", path)
        return path
    except OSError as exc:
        logger.debug("External subagent observer did not start: %s", exc)
        return None


def _serve_loop(sock: socket.socket) -> None:
    while True:
        try:
            conn, _addr = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=_serve_conn, args=(conn,), daemon=True).start()


def stop_observer() -> None:
    with _external_lock:
        sock, thread, path = _server_state["sock"], _server_state["thread"], _server_state["path"]
        _server_state.update({"thread": None, "sock": None, "path": None})
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    del thread


def clear_external_records() -> int:
    """Test helper: drop all external records. Returns how many were removed."""
    with _external_lock:
        n = len(_external_records)
        _external_records.clear()
    return n


def external_records_snapshot(now: Optional[float] = None) -> list:
    """Copy of live external records (public fields only), pruned of stale entries
    (launcher died without unregistering). Records are sorted oldest-updated first."""
    now = now or time.time()
    with _external_lock:
        recs = [dict(r) for r in _external_records.values()
                if now - (r.get("_updated_at") or 0) <= _EXTERNAL_STALE_SECONDS]
    recs.sort(key=lambda r: r.get("_updated_at") or 0)
    for r in recs:
        r.pop("_updated_at", None)
    return recs


def merge_external_into(records: list) -> list:
    """Append live external records to a ``list_active_subagents()`` result. Pure w.r.t.
    *records*. Status normalization: 'killed' maps onto 'interrupted', which is the
    closest TUI SubagentStatus vocabulary entry for a launcher-initiated stop."""
    out = list(records)
    for rec in external_records_snapshot():
        status = rec.get("status") or "running"
        if status == "killed":
            status = "interrupted"
        out.append({**rec, "status": status})
    return out


def prune_external_records(now: Optional[float] = None) -> int:
    with _external_lock:
        now = now or time.time()
        stale = [sid for sid, r in _external_records.items()
                 if now - (r.get("_updated_at") or 0) > _EXTERNAL_STALE_SECONDS]
        for sid in stale:
            _external_records.pop(sid, None)
        return len(stale)
