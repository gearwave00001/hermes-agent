#!/usr/bin/env python3
"""
Subagent Router — Priority-based provider routing with check-out counters.

Opt-in feature activated by the ``subagent_routing:`` block in config.yaml.
When absent, delegate_tool.py uses the existing single-provider path unchanged.

Provides:
  - Priority-based routing across multiple local GPU servers
  - Per-provider concurrency counters (max_concurrent)
  - Health checks before assignment (configurable timeout)
  - Rule-based routing via goal substring matching (goal_rules)
  - Per-task provider override (tasks[].provider)
  - Overflow queue with background dispatcher thread
  - Sync queuing support (parent blocks until all queued tasks complete)

Thread safety:
  All mutable state is protected by ``_state_lock`` (threading.Lock).
  The dispatcher thread polls every ``poll_interval`` seconds.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Per-provider active connection counts.
# Key: provider name (as it appears in custom_providers), value: int
_active_counts: Dict[str, int] = {}

# Global lock protecting _active_counts and _pending_queue
_state_lock = threading.Lock()

# Overflow queue: list of dicts with task metadata and captured context.
# Each entry: {task, session_key, sync_event, parent_agent, delegation_cfg}
# parent_agent and delegation_cfg are captured at enqueue time so the
# dispatcher can build child agents without needing the parent alive.
_pending_queue: List[dict] = []

# Dispatcher thread handle (singleton, started on first use)
_dispatcher_thread: Optional[threading.Thread] = None
_dispatcher_running = False

# Queue config (loaded lazily)
_queue_config: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_subagent_routing_config() -> dict:
    """Load the subagent_routing block from config.yaml.

    Returns the raw dict, or empty dict if not configured.
    """
    try:
        from cli import CLI_CONFIG

        cfg = CLI_CONFIG.get("subagent_routing") or {}
        if cfg:
            return cfg
    except Exception:
        pass
    try:
        from hermes_cli.config import load_config

        full = load_config()
        return full.get("subagent_routing") or {}
    except Exception:
        return {}


def _is_enabled() -> bool:
    """Return True when subagent_routing is enabled in config."""
    cfg = _load_subagent_routing_config()
    return bool(cfg.get("enabled", False))


def _get_priority_order() -> List[dict]:
    """Return the priority_order list from config."""
    cfg = _load_subagent_routing_config()
    return cfg.get("priority_order", [])


def _get_goal_rules() -> List[dict]:
    """Return the goal_rules list from config."""
    cfg = _load_subagent_routing_config()
    return cfg.get("goal_rules", [])


def _get_health_check_timeout() -> float:
    """Return the health check timeout in seconds (default 3)."""
    cfg = _load_subagent_routing_config()
    return float(cfg.get("health_check_timeout", 3))


def _get_queue_config() -> dict:
    """Return the queue config block (cached)."""
    global _queue_config
    if not _queue_config:
        cfg = _load_subagent_routing_config()
        _queue_config = cfg.get("queue", {})
    return _queue_config


def _is_queue_enabled() -> bool:
    """Return True when the overflow queue is enabled."""
    qc = _get_queue_config()
    return bool(qc.get("enabled", True))


def _get_queue_max_size() -> int:
    """Return the maximum queue size (default 20)."""
    qc = _get_queue_config()
    return int(qc.get("max_size", 20))


def _get_poll_interval() -> float:
    """Return the dispatcher poll interval in seconds (default 2)."""
    qc = _get_queue_config()
    return float(qc.get("poll_interval", 2))


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def _health_check(base_url: str, timeout: float) -> bool:
    """Ping a provider's /v1/models endpoint to verify availability.

    Returns True if the endpoint responds within the timeout, False otherwise.
    """
    try:
        url = base_url.rstrip("/") + "/models"
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
            return resp.status_code == 200
    except Exception as exc:
        logger.debug("Health check failed for %s: %s", base_url, exc)
        return False


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------

def _resolve_provider_credentials(provider_name: str) -> Optional[dict]:
    """Resolve full credentials for a named custom provider.

    Returns a dict with {provider, model, base_url, api_key, api_mode} or None.
    """
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested=provider_name)
        api_key = runtime.get("api_key", "")
        if not api_key:
            logger.warning(
                "Provider '%s' resolved but has no API key", provider_name
            )
            return None

        return {
            "provider": provider_name
            if runtime.get("provider") == "custom"
            else runtime.get("provider"),
            "model": runtime.get("model"),
            "base_url": runtime.get("base_url"),
            "api_key": api_key,
            "api_mode": runtime.get("api_mode"),
            "command": runtime.get("command"),
            "args": list(runtime.get("args") or []),
        }
    except Exception as exc:
        logger.warning("Failed to resolve provider '%s': %s", provider_name, exc)
        return None


# ---------------------------------------------------------------------------
# Goal rule matching
# ---------------------------------------------------------------------------

def _match_goal_rule(goal: str) -> Optional[str]:
    """Check if a goal matches any goal_rules entry.

    Returns the provider name to use, or None if no rule matches.
    """
    rules = _get_goal_rules()
    if not rules or not goal:
        return None

    goal_lower = goal.lower()
    for rule in rules:
        match_pattern = str(rule.get("match", "")).strip().lower()
        if not match_pattern:
            continue
        provider = str(rule.get("provider", "")).strip()
        if not provider:
            continue
        # Support substring match and regex (if pattern starts with ^)
        if match_pattern.startswith("^"):
            try:
                if re.search(match_pattern, goal_lower):
                    return provider
            except re.error:
                logger.warning("Invalid regex in goal_rules: %s", match_pattern)
        else:
            if match_pattern in goal_lower:
                return provider

    return None


# ---------------------------------------------------------------------------
# Acquire / Release (check-out / check-in)
# ---------------------------------------------------------------------------

def acquire_provider(
    provider_override: Optional[str] = None,
    goal: Optional[str] = None,
) -> Optional[dict]:
    """Acquire a provider for a subagent task.

    Priority order:
      1. provider_override (explicit per-task override)
      2. goal_rules match (config-based rule matching)
      3. priority_order routing (first available server)

    Returns credential dict or None if no provider is available.
    """
    # Path 1: explicit override
    if provider_override:
        creds = _resolve_provider_credentials(provider_override)
        if creds:
            with _state_lock:
                _active_counts[provider_override] = (
                    _active_counts.get(provider_override, 0) + 1
                )
            logger.debug(
                "Acquired provider '%s' (explicit override), "
                "active_count=%d",
                provider_override,
                _active_counts[provider_override],
            )
            return creds

    # Path 2: goal-based rule matching
    rule_provider = _match_goal_rule(goal or "")
    if rule_provider:
        creds = _resolve_provider_credentials(rule_provider)
        if creds:
            with _state_lock:
                _active_counts[rule_provider] = (
                    _active_counts.get(rule_provider, 0) + 1
                )
            logger.debug(
                "Acquired provider '%s' (goal rule match), "
                "active_count=%d",
                rule_provider,
                _active_counts[rule_provider],
            )
            return creds

    # Path 3: priority_order routing
    if not _is_enabled():
        return None  # fall through to default delegation.provider

    priority_order = _get_priority_order()
    if not priority_order:
        return None

    health_timeout = _get_health_check_timeout()

    for entry in priority_order:
        name = entry.get("name", "")
        if not name:
            continue

        # Check enabled flag
        if not entry.get("enabled", True):
            continue

        # Check max_concurrent (REQUIRED — skip if missing)
        max_conc = entry.get("max_concurrent")
        if max_conc is None:
            logger.warning(
                "Provider '%s' missing required max_concurrent — skipping",
                name,
            )
            continue

        with _state_lock:
            current = _active_counts.get(name, 0)
            if current >= int(max_conc):
                continue  # at capacity, try next

        # Health check
        creds = _resolve_provider_credentials(name)
        if not creds:
            continue

        if health_timeout > 0:
            if not _health_check(creds["base_url"], health_timeout):
                logger.debug(
                    "Provider '%s' failed health check — skipping", name
                )
                continue

        # Acquire!
        with _state_lock:
            _active_counts[name] = _active_counts.get(name, 0) + 1

        logger.debug(
            "Acquired provider '%s' (priority routing), "
            "active_count=%d/%d",
            name,
            _active_counts[name],
            max_conc,
        )
        return creds

    return None  # all providers at capacity


def release_provider(provider_name: str) -> None:
    """Release a provider after subagent completion.

    Decrements active_count and triggers queue dispatch if pending tasks exist.
    """
    with _state_lock:
        current = _active_counts.get(provider_name, 0)
        if current > 0:
            _active_counts[provider_name] = current - 1
            logger.debug(
                "Released provider '%s', active_count=%d",
                provider_name,
                _active_counts[provider_name],
            )
        else:
            logger.warning(
                "release_provider called for '%s' with active_count=0",
                provider_name,
            )

        # Trigger queue dispatch if there are pending tasks
        if _pending_queue:
            _try_dispatch_queued()


def get_active_counts() -> Dict[str, int]:
    """Return a snapshot of current active counts per provider."""
    with _state_lock:
        return dict(_active_counts)


# ---------------------------------------------------------------------------
# Queue management
# ---------------------------------------------------------------------------

def enqueue_task(
    *,
    task: dict,
    session_key: str,
    sync_event: Optional[threading.Event],
    parent_agent: Any,
    delegation_cfg: dict,
) -> bool:
    """Add a task to the overflow queue.

    Captures parent_agent and delegation_cfg at enqueue time so the
    dispatcher can build child agents later without the parent still alive.

    Returns True if enqueued, False if queue is full.
    """
    if not _is_queue_enabled():
        return False

    max_size = _get_queue_max_size()

    with _state_lock:
        if len(_pending_queue) >= max_size:
            logger.warning(
                "Subagent queue full (%d/%d) — cannot enqueue task: %s",
                len(_pending_queue),
                max_size,
                task.get("goal", "")[:80],
            )
            return False

        entry = {
            "task": task,
            "session_key": session_key,
            "sync_event": sync_event,
            "parent_agent": parent_agent,
            "delegation_cfg": delegation_cfg,
        }
        _pending_queue.append(entry)
        logger.debug(
            "Enqueued task '%s' (queue size: %d/%d)",
            task.get("goal", "")[:80],
            len(_pending_queue),
            max_size,
        )

    # Ensure dispatcher thread is running
    _ensure_dispatcher()

    return True


def _try_dispatch_queued() -> None:
    """Try to dispatch pending tasks from the queue.

    Called from release_provider() after a slot opens up.
    Must be called with _state_lock held.
    Spawns a background thread to do the actual dispatch (health checks,
    provider resolution) so we don't block the releasing thread.
    """
    if not _pending_queue:
        return

    # Snapshot the queue and clear it — the dispatcher thread will
    # put back anything it couldn't dispatch.
    tasks_snapshot = list(_pending_queue)
    _pending_queue.clear()

    # Dispatch in a background thread to avoid blocking
    threading.Thread(
        target=_dispatch_queued_unlocked,
        args=(tasks_snapshot,),
        daemon=True,
        name="subagent-router-dispatch",
    ).start()


def _dispatch_queued_unlocked(entries: List[dict]) -> None:
    """Dispatch queued entries without holding the global lock.

    For each entry, tries to acquire a provider. If successful, builds
    and dispatches the child agent. If not, puts the entry back in the queue.
    """
    dispatched_entries = []
    remaining_entries = []

    for entry in entries:
        task = entry["task"]
        provider_override = task.get("provider")
        goal = task.get("goal", "")

        creds = acquire_provider(
            provider_override=provider_override,
            goal=goal,
        )

        if creds:
            entry["creds"] = creds
            dispatched_entries.append(entry)
        else:
            remaining_entries.append(entry)

    # Actually dispatch the ones we got providers for
    for entry in dispatched_entries:
        try:
            _dispatch_single_queued(entry)
        except Exception as exc:
            logger.error(
                "Failed to dispatch queued task '%s': %s",
                entry["task"].get("goal", "")[:80],
                exc,
            )
            # Release the provider we acquired since we couldn't dispatch
            creds = entry.get("creds", {})
            provider_name = entry["task"].get("provider") or ""
            if not provider_name:
                # Try to figure out which provider we got
                for name, count in get_active_counts().items():
                    if count > 0:
                        provider_name = name
                        break
            if provider_name:
                release_provider(provider_name)
            remaining_entries.append(entry)

    # Put remaining entries back in the queue
    if remaining_entries:
        with _state_lock:
            _pending_queue.extend(remaining_entries)


def _dispatch_single_queued(entry: dict) -> None:
    """Dispatch a single queued entry as a background subagent.

    Uses the captured parent_agent and delegation_cfg from enqueue time.
    """
    from tools.async_delegation import dispatch_async_delegation
    from tools.delegate_tool import (
        _build_child_agent,
        _get_max_async_children,
        _normalize_role,
        _run_single_child,
    )
    from tools.approval import get_current_session_key

    task = entry["task"]
    session_key = entry["session_key"]
    sync_event = entry["sync_event"]
    parent_agent = entry["parent_agent"]
    creds = entry["creds"]

    goal = task.get("goal", "")
    context = task.get("context")
    toolsets = task.get("toolsets")
    role = _normalize_role(task.get("role") or "leaf")

    delegation_cfg = entry["delegation_cfg"]
    max_iter = delegation_cfg.get("max_iterations", 50)

    # Build the child agent using the same path as delegate_tool.py
    child = _build_child_agent(
        task_index=0,
        goal=goal,
        context=context,
        toolsets=toolsets,
        model=creds.get("model"),
        max_iterations=max_iter,
        task_count=1,
        parent_agent=parent_agent,
        override_provider=creds.get("provider"),
        override_base_url=creds.get("base_url"),
        override_api_key=creds.get("api_key"),
        override_api_mode=creds.get("api_mode"),
        override_acp_command=creds.get("command"),
        override_acp_args=creds.get("args", []),
        role=role,
    )

    def _runner(_child=child, _goal=goal):
        return _run_single_child(0, _goal, _child, parent_agent)

    def _interrupt(_child=child):
        try:
            if hasattr(_child, "interrupt"):
                _child.interrupt("Queued subagent cancelled")
            elif hasattr(_child, "_interrupt_requested"):
                _child._interrupt_requested = True
        except Exception:
            pass

    dispatch = dispatch_async_delegation(
        goal=goal,
        context=context,
        toolsets=toolsets,
        role=role,
        model=creds.get("model"),
        session_key=session_key,
        runner=_runner,
        interrupt_fn=_interrupt,
        max_async_children=_get_max_async_children(),
    )

    if dispatch.get("status") == "dispatched":
        logger.debug(
            "Dispatched queued task '%s' as async delegation %s",
            goal[:80],
            dispatch["delegation_id"],
        )
        # Signal sync event if this was a synchronous delegation
        if sync_event:
            sync_event.set()
    else:
        logger.warning(
            "Failed to dispatch queued task '%s': %s",
            goal[:80],
            dispatch.get("error", "unknown"),
        )
        if sync_event:
            sync_event.set()

# ---------------------------------------------------------------------------
# Dispatcher thread
# ---------------------------------------------------------------------------

def _ensure_dispatcher() -> None:
    """Start the dispatcher thread if not already running."""
    global _dispatcher_thread, _dispatcher_running

    with _state_lock:
        if _dispatcher_running and _dispatcher_thread and _dispatcher_thread.is_alive():
            return

        _dispatcher_running = True
        _dispatcher_thread = threading.Thread(
            target=_dispatcher_loop,
            daemon=True,
            name="subagent-router-dispatcher",
        )
        _dispatcher_thread.start()
        logger.debug("Started subagent router dispatcher thread")


def _dispatcher_loop() -> None:
    """Background dispatcher loop.

    Polls the queue at regular intervals and dispatches tasks when providers
    become available.
    """
    poll_interval = _get_poll_interval()

    while _dispatcher_running:
        try:
            with _state_lock:
                if not _pending_queue:
                    # No pending tasks — check if we should stop
                    # Keep running as long as _dispatcher_running is True
                    pass
                else:
                    # Try to dispatch
                    tasks_snapshot = list(_pending_queue)
                    _pending_queue.clear()

            if tasks_snapshot:
                _dispatch_queued_unlocked(tasks_snapshot)

        except Exception as exc:
            logger.error("Dispatcher loop error: %s", exc)

        time.sleep(poll_interval)


def stop_dispatcher() -> None:
    """Stop the dispatcher thread.

    Called during shutdown or when subagent_routing is disabled.
    """
    global _dispatcher_running
    _dispatcher_running = False


def reset_state() -> None:
    """Reset all routing state.

    Useful for testing or when subagent_routing config changes.
    """
    global _active_counts, _pending_queue, _queue_config
    with _state_lock:
        _active_counts.clear()
        _pending_queue.clear()
    _queue_config = {}
    stop_dispatcher()


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------

def get_status() -> dict:
    """Return current routing status for monitoring/debugging.

    Returns a dict with:
      - enabled: bool
      - active_counts: {provider: count}
      - queue_size: int
      - queue_max: int
      - dispatcher_running: bool
      - priority_order: list of {name, enabled, max_concurrent, active_count}
    """
    priority_order = _get_priority_order()
    active = get_active_counts()

    with _state_lock:
        queue_size = len(_pending_queue)

    provider_status = []
    for entry in priority_order:
        name = entry.get("name", "")
        provider_status.append({
            "name": name,
            "enabled": entry.get("enabled", True),
            "max_concurrent": entry.get("max_concurrent"),
            "active_count": active.get(name, 0),
        })

    return {
        "enabled": _is_enabled(),
        "active_counts": active,
        "queue_size": queue_size,
        "queue_max": _get_queue_max_size(),
        "queue_enabled": _is_queue_enabled(),
        "dispatcher_running": _dispatcher_running,
        "priority_order": provider_status,
        "goal_rules": _get_goal_rules(),
    }
