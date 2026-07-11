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
  All mutable state is protected by the singleton state's lock
  (threading.Lock). The dispatcher thread polls every ``poll_interval``
  seconds.

Reimport safety:
  All state lives in a _RouterState singleton whose class-level _instance
  survives module reimport (config reload, model switch, etc.). Plain
  module-level variables would reset to their initial value on reimport,
  killing the dispatcher thread and wiping the pending queue.
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
# Process-global singleton state (survives module reimport)
# ---------------------------------------------------------------------------


class _RouterState:
    """Process-global singleton that survives module reimport.

    Module-level variables reset on reimport (config reload, model switch,
    etc.), which kills the dispatcher thread and wipes the pending queue.
    This class stores its canonical instance at the class level, so a
    reimport of the module creates a *new class object* but the old
    instance (and its data) remains reachable via the class that the
    dispatcher thread still holds a reference to.

    To make it even more robust, we also store a reference in sys.modules
    under a stable key, so even if the old class object is garbage collected,
    the data survives.
    """
    # Class-level attribute declarations for type checker satisfaction
    _active_counts: Dict[str, int]
    _pending_queue: List[dict]
    _dispatcher_thread: Optional[threading.Thread]
    _dispatcher_running: bool
    _lock: threading.Lock
    _dispatching: bool  # Guard against concurrent dispatch from release_provider + dispatcher loop
    _thread_id: int  # Monotonically increasing ID; tracks which dispatcher thread is "ours"
    _thread_id_counter: int  # Counter for generating unique thread IDs

    _instance: "Optional[_RouterState]" = None  # type: ignore[valid-type]

    def __new__(cls):
        # Try to recover from sys.modules first (survives class reimport).
        # We check the marker key AND that the object has our sentinel attrs,
        # not isinstance (which fails against a reloaded class).
        import sys as _sys

        stored = _sys.modules.get("__hermes_router_state__")  # type: ignore[call-overload]
        if stored is not None and hasattr(stored, "_active_counts"):
            return stored  # type: ignore[return-value]

        if cls._instance is not None:
            return cls._instance

        instance = super().__new__(cls)
        # Initialize attributes (type: ignore needed since __new__ returns object)
        instance._active_counts = {}  # type: ignore[assignment]
        instance._pending_queue = []  # type: ignore[assignment]
        instance._dispatcher_thread = None  # type: ignore[assignment]
        instance._dispatcher_running = False  # type: ignore[assignment]
        instance._lock = threading.Lock()  # type: ignore[assignment]
        instance._dispatching = False  # type: ignore[assignment]
        instance._thread_id = 0  # type: ignore[assignment]
        instance._thread_id_counter = 0  # type: ignore[assignment]
        cls._instance = instance
        # Also store in sys.modules for extra reimport resilience
        _sys.modules["__hermes_router_state__"] = instance  # type: ignore[arg-type]
        return instance

    @classmethod
    def force_reset(cls) -> None:
        """Hard reset -- only for tests. Clears the singleton."""
        import sys as _sys

        cls._instance = None
        _sys.modules.pop("__hermes_router_state__", None)


def _state() -> _RouterState:
    """Get the singleton state instance."""
    return _RouterState()


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


def _get_main_server() -> str:
    """Return the main_server name from config (empty string if not set)."""
    cfg = _load_subagent_routing_config()
    return cfg.get("main_server", "")


def _should_exclude_from_subagents() -> bool:
    """Return True if the main_server should be excluded from subagent routing."""
    cfg = _load_subagent_routing_config()
    return bool(cfg.get("exclude_from_subagents", False))


def _is_excluded_provider(name: str) -> bool:
    """Return True if this provider should be excluded from subagent routing.

    When main_server is set and exclude_from_subagents is True, that provider
    is skipped during subagent acquisition so it stays available for the
    parent agent's own API calls.
    """
    if not _should_exclude_from_subagents():
        return False
    main = _get_main_server()
    return bool(main and name == main)


def _get_queue_config() -> dict:
    """Return the queue config block.

    Re-reads from config on each call (not cached) so that runtime config
    changes (e.g. via /config or hermes config set) are picked up immediately.
    The config loading itself is fast (dict merge) and called infrequently
    (only during enqueue/dispatch), so caching provides no meaningful benefit
    but causes stale-state bugs when config changes mid-session.
    """
    cfg = _load_subagent_routing_config()
    return cfg.get("queue", {})


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
    # Ensure the dispatcher thread is alive before any provider acquisition.
    # This catches the case where the dispatcher died (module reimport,
    # exception, config reload) and queued tasks are sitting idle.
    # Idempotent when the dispatcher is already running.
    _ensure_dispatcher()

    st = _state()

    # Path 1: explicit override
    if provider_override:
        if _is_excluded_provider(provider_override):
            logger.debug("Provider '%s' excluded (main_server) — skipping override", provider_override)
        else:
            creds = _resolve_provider_credentials(provider_override)
            if creds:
                with st._lock:
                    st._active_counts[provider_override] = (
                        st._active_counts.get(provider_override, 0) + 1
                    )
                logger.debug(
                    "Acquired provider '%s' (explicit override), "
                    "active_count=%d",
                    provider_override,
                    st._active_counts[provider_override],
                )
                return creds

    # Path 2: goal-based rule matching
    rule_provider = _match_goal_rule(goal or "")
    if rule_provider:
        if _is_excluded_provider(rule_provider):
            logger.debug("Provider '%s' excluded (main_server) — skipping goal rule", rule_provider)
        else:
            creds = _resolve_provider_credentials(rule_provider)
            if creds:
                with st._lock:
                    st._active_counts[rule_provider] = (
                        st._active_counts.get(rule_provider, 0) + 1
                    )
                logger.debug(
                    "Acquired provider '%s' (goal rule match), "
                    "active_count=%d",
                    rule_provider,
                    st._active_counts[rule_provider],
                )
                return creds

    # Path 3: priority_order routing - pick server with most remaining capacity.
    # Instead of "first in list that has room" (which can stack subagents on a
    # slow early-listed server while later servers sit idle), we scan all eligible
    # servers and sort by remaining capacity (descending). Ties break by
    # priority_order position (first in list wins). We loop through the sorted
    # list, reserving each slot before health-checking it — if one fails, we
    # try the next-best rather than giving up entirely.
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
        if not entry.get("enabled", True):
            continue
        if _is_excluded_provider(name):
            logger.debug("Provider '%s' excluded (main_server) — skipping", name)
            continue
        max_conc = entry.get("max_concurrent")
        if max_conc is None:
            continue

        with st._lock:
            current = st._active_counts.get(name, 0)
            if current >= max_conc:
                continue
            st._active_counts[name] = current + 1

        # Health check runs OUTSIDE the lock; slot is already reserved.
        creds = _resolve_provider_credentials(name)
        if not creds:
            with st._lock:
                del st._active_counts[name]
            continue

        if health_timeout > 0:
            if not _health_check(creds["base_url"], health_timeout):
                logger.debug(
                    "Provider '%s' failed health check -- trying next", name
                )
                with st._lock:
                    del st._active_counts[name]
                continue

        logger.info(
            "Acquired provider '%s' (capacity-aware routing), "
            "active_count=%d/%d",
            name,
            st._active_counts[name],
            max_conc,
        )
        return creds

    return None  # all providers at capacity or none eligible


def release_provider(provider_name: str, skip_queue_dispatch: bool = False) -> None:
    """Release a provider after subagent completion.

    Decrements active_count and triggers queue dispatch if pending tasks exist.

    LIVENESS CHECK: Always ensures the dispatcher thread is alive after
    releasing a slot. If the dispatcher died (module reimport, exception,
    config reload), we restart it AND dispatch any queued tasks inline.
    This prevents the recurring issue where the dispatcher stays dead
    indefinitely after a subagent completes, leaving future queued tasks
    orphaned.

    NOTE: When skip_queue_dispatch=True (used internally by
    _dispatch_queued_unlocked's error path), we only decrement the counter
    without triggering queue dispatch — this prevents nested dispatch
    cascades where releasing a slot during dispatch spawns new dispatch
    threads that re-dispatch the same remaining entries.
    """
    st = _state()
    tasks_snapshot: List[dict] = []
    need_dispatcher_restart = False

    with st._lock:
        current = st._active_counts.get(provider_name, 0)
        if current > 0:
            st._active_counts[provider_name] = current - 1
            logger.info(
                "Released provider '%s', active_count=%d",
                provider_name,
                st._active_counts[provider_name],
            )
        else:
            logger.warning(
                "release_provider called for '%s' with active_count=0",
                provider_name,
            )

        # Skip queue dispatch when called from _dispatch_queued_unlocked
        # error path — prevents nested dispatch cascade.
        if skip_queue_dispatch:
            return

        # Check if dispatcher needs restart (while holding lock).
        # We do the actual restart OUTSIDE the lock to avoid deadlock:
        # _ensure_dispatcher() also acquires the lock, and we don't want
        # to hold the lock while calling it.
        dispatcher_alive = (
            st._dispatcher_running
            and st._dispatcher_thread is not None
            and st._dispatcher_thread.is_alive()
        )

        if not dispatcher_alive:
            need_dispatcher_restart = True

        # Snapshot pending tasks for inline dispatch (if dispatcher was dead).
        # The newly restarted dispatcher will handle future tasks.
        if st._pending_queue:
            if not dispatcher_alive:
                logger.warning(
                    "Dispatcher was dead with %d pending tasks -- "
                    "dispatching inline from release_provider",
                    len(st._pending_queue),
                )
                tasks_snapshot = list(st._pending_queue)
                st._pending_queue.clear()
            else:
                # Normal path: dispatcher is alive, let it handle it.
                # _try_dispatch_queued() expects the caller to hold the lock.
                _try_dispatch_queued()

    # --- Outside the lock now ---

    # Restart dispatcher if needed (this acquires the lock internally).
    if need_dispatcher_restart:
        logger.warning("Dispatcher thread is dead — restarting from release_provider")
        _ensure_dispatcher()

    # Dispatch inline snapshot (if any) — runs in its own thread.
    if tasks_snapshot:
        threading.Thread(
            target=_dispatch_queued_unlocked,
            args=(tasks_snapshot,),
            daemon=True,
            name="subagent-router-inline-dispatch",
        ).start()


def get_active_counts() -> Dict[str, int]:
    """Return a snapshot of current active counts per provider."""
    st = _state()
    with st._lock:
        return dict(st._active_counts)


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
    st = _state()

    with st._lock:
        if len(st._pending_queue) >= max_size:
            logger.warning(
                "Subagent queue full (%d/%d) -- cannot enqueue task: %s",
                len(st._pending_queue),
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
        st._pending_queue.append(entry)
        logger.debug(
            "Enqueued task '%s' (queue size: %d/%d)",
            task.get("goal", "")[:80],
            len(st._pending_queue),
            max_size,
        )

    # Ensure dispatcher thread is running
    _ensure_dispatcher()

    return True


def _try_dispatch_queued() -> None:
    """Try to dispatch pending tasks from the queue.

    Called from release_provider() after a slot opens up (lock held).
    Sets a _dispatching flag so the dispatcher loop doesn't double-snapshot
    the same entries — prevents duplicate dispatches when both paths fire
    close together (e.g., two subagents finish within the same poll window).
    """
    st = _state()
    if not st._pending_queue:
        return

    # Guard against race with dispatcher loop: if another thread is already
    # dispatching (from release_provider or the loop itself), skip this call.
    # This prevents both paths from snapshotting the same queue entries.
    if st._dispatching:
        return

    # Mark as dispatching before clearing the queue
    st._dispatching = True
    tasks_snapshot = list(st._pending_queue)
    st._pending_queue.clear()

    # Release lock before spawning dispatch thread (caller holds it)
    threading.Thread(
        target=_dispatch_queued_with_flag,
        args=(tasks_snapshot,),
        daemon=True,
        name="subagent-router-dispatch",
    ).start()


def _dispatch_queued_with_flag(entries: List[dict]) -> None:
    """Wrapper around _dispatch_queued_unlocked that clears _dispatching.

    Used by _try_dispatch_queued (called from release_provider with lock held).
    The dispatcher loop now manages _dispatching directly (sets before snapshot,
    clears in finally), so this wrapper is only needed for the release_provider path.
    """
    try:
        _dispatch_queued_unlocked(entries)
    finally:
        st = _state()
        with st._lock:
            st._dispatching = False


def _dispatch_queued_unlocked(entries: List[dict]) -> None:
    """Dispatch queued entries without holding the global lock.

    For each entry, tries to acquire a provider. If successful, builds
    and dispatches the child agent. If not, puts the entry back in the queue.

    NOTE: This function is called from two paths:
      1. _try_dispatch_queued (from release_provider, via _dispatch_queued_with_flag)
      2. _dispatcher_loop (directly, with _dispatching managed by the loop)
      3. _ensure_dispatcher inline dispatch (via separate thread)
      4. release_provider inline dispatch (via separate thread)

    The _dispatching flag prevents paths 1+2 from racing. Paths 3+4 run in
    separate threads so they don't block the main flow.
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
            # Release the provider we acquired since we couldn't dispatch.
            # entry["creds"] was set above when acquire_provider succeeded,
            # so we know the exact provider that was reserved.
            creds = entry.get("creds", {})
            provider_name = entry["task"].get("provider") or creds.get("provider", "")
            if not provider_name:
                # Fallback: match base_url against known providers
                for po_entry in _get_priority_order():
                    po_name = po_entry.get("name", "")
                    resolved = _resolve_provider_credentials(po_name)
                    if resolved and resolved.get("base_url") == creds.get("base_url"):
                        provider_name = po_name
                        break
            if provider_name:
                # Use non-cascading release to prevent nested dispatch:
                # releasing a slot here would trigger queue dispatch, which
                # would re-dispatch the same remaining entries we're already
                # processing, creating exponential thread explosion.
                release_provider(provider_name, skip_queue_dispatch=True)
            remaining_entries.append(entry)

    # Put remaining entries back in the queue
    if remaining_entries:
        st = _state()
        with st._lock:
            st._pending_queue.extend(remaining_entries)


def _dispatch_single_queued(entry: dict) -> None:
    """Dispatch a single queued entry as a background subagent.

    Uses the captured parent_agent and delegation_cfg from enqueue time.
    Sets _assigned_provider on the child so release_provider() is called
    correctly when the subagent completes (enqueued tasks had child_creds=None
    at enqueue time, so _assigned_provider was never set).
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

    # Determine the provider name for release tracking.
    # For priority-routed providers, this is the server IP/name from config.
    assigned_provider = task.get("provider") or creds.get("provider", "")
    if not assigned_provider:
        # Fallback: try to match base_url against known providers
        for entry_cfg in _get_priority_order():
            name = entry_cfg.get("name", "")
            resolved = _resolve_provider_credentials(name)
            if resolved and resolved.get("base_url") == creds.get("base_url"):
                assigned_provider = name
                break

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

    # CRITICAL: Set _assigned_provider so release_provider() is called on completion.
    # Enqueued tasks had child_creds=None at enqueue time, so _assigned_provider
    # was never set in the main dispatch loop. Without this, the provider slot
    # is never released, causing phantom capacity that blocks future dispatches.
    setattr(child, "_assigned_provider", assigned_provider)

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
        logger.info(
            "Dispatched queued task '%s' as async delegation %s (provider=%s)",
            goal[:80],
            dispatch["delegation_id"],
            assigned_provider,
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
    """Start the dispatcher thread if not already running.

    AUTO-RESTART: If the dispatcher was previously running but has died
    (e.g. due to module reimport), this detects the dead state and
    restarts the thread. If there are pending tasks, it also triggers
    an immediate inline dispatch.

    NOTE: This is called from acquire_provider (which runs from both the
    main dispatch loop AND from _dispatch_queued_unlocked). The inline
    dispatch runs in a separate thread to avoid blocking the caller.

    THREAD SAFETY: Uses _thread_id to track which dispatcher thread is
    "ours". If two threads both see the dispatcher as dead, the second
    one checks the thread ID and realizes someone else already started
    a new thread, preventing duplicate dispatchers.
    """
    st = _state()
    tasks_snapshot: List[dict] = []

    with st._lock:
        # Fast path: dispatcher is alive
        if st._dispatcher_running and st._dispatcher_thread and st._dispatcher_thread.is_alive():
            return

        # Check if another thread already started a new dispatcher while we
        # were waiting for the lock (thread ID is the authoritative marker).
        if st._thread_id is not None and st._dispatcher_thread is not None and st._dispatcher_thread.is_alive():
            return

        # Dispatcher is dead or never started -- restart it
        if not st._dispatcher_running and st._pending_queue:
            logger.warning(
                "Dispatcher was dead with %d pending tasks -- restarting and dispatching inline",
                len(st._pending_queue),
            )
            # Snapshot and clear for inline dispatch
            tasks_snapshot = list(st._pending_queue)
            st._pending_queue.clear()

        thread_id = st._thread_id_counter + 1  # Monotonically increasing unique ID
        # Set _thread_id BEFORE starting the thread so the new thread sees
        # its own ID on the first loop iteration (not the stale value from
        # force_reset). If we set it after start(), the thread could check
        # _thread_id before we update it and exit immediately.
        st._thread_id = thread_id
        st._thread_id_counter = thread_id
        st._dispatcher_running = True
        st._dispatcher_thread = threading.Thread(
            target=_dispatcher_loop,
            args=(thread_id,),
            daemon=True,
            name="subagent-router-dispatcher",
        )
        st._dispatcher_thread.start()
        logger.debug("Started subagent router dispatcher thread (id=%s)", thread_id)

    # If we had pending tasks, dispatch them inline immediately
    # (in a separate thread so we don't block the caller)
    if tasks_snapshot:
        threading.Thread(
            target=_dispatch_queued_unlocked,
            args=(tasks_snapshot,),
            daemon=True,
            name="subagent-router-restart-dispatch",
        ).start()


def _dispatcher_loop(my_thread_id: int) -> None:
    """Background dispatcher loop.

    Polls the queue at regular intervals and dispatches tasks when providers
    become available.

    Guards against racing with _try_dispatch_queued (called from
    release_provider) by checking the _dispatching flag before snapshotting.

    Self-identification: The loop checks _thread_id on each iteration. If
    another thread has replaced us (new _thread_id != my_thread_id), we exit
    gracefully instead of continuing as a stale dispatcher.
    """
    while True:
        st = _state()

        # Self-identification check: if someone replaced us, exit cleanly.
        if st._thread_id != my_thread_id:
            logger.debug(
                "Dispatcher loop exiting (replaced: my_id=%s, current_id=%s)",
                my_thread_id, st._thread_id,
            )
            break

        if not st._dispatcher_running:
            break

        poll_interval = _get_poll_interval()
        tasks_snapshot: list[dict] = []  # Declared inside try to avoid stale state

        try:
            with st._lock:
                if st._pending_queue and not st._dispatching:
                    # Snapshot and clear the queue for dispatch.
                    # Check _dispatching to avoid racing with _try_dispatch_queued
                    # (called from release_provider). If another thread is already
                    # dispatching, skip this poll cycle — it will pick up remaining
                    # tasks on the next iteration.
                    st._dispatching = True
                    tasks_snapshot = list(st._pending_queue)
                    st._pending_queue.clear()
                else:
                    tasks_snapshot = []  # Reset when queue is empty or dispatching

            if tasks_snapshot:
                try:
                    _dispatch_queued_unlocked(tasks_snapshot)
                finally:
                    with st._lock:
                        st._dispatching = False

        except Exception as exc:
            logger.error("Dispatcher loop error: %s", exc)
            # Ensure _dispatching is cleared on any exception
            with st._lock:
                st._dispatching = False

        time.sleep(poll_interval)


def stop_dispatcher() -> None:
    """Stop the dispatcher thread.

    Called during shutdown or when subagent_routing is disabled.
    Waits for the thread to actually exit (up to 5s) to prevent stale
    daemon threads from racing with newly started dispatchers.
    """
    st = _state()
    st._dispatcher_running = False
    st._thread_id = 0
    # Wait for the thread to actually exit to prevent stale daemon threads
    # from racing with newly started dispatchers (e.g. after force_reset).
    if st._dispatcher_thread is not None:
        st._dispatcher_thread.join(timeout=5)


def reset_state() -> None:
    """Reset all routing state.

    Useful for testing or when subagent_routing config changes.

    SAFETY: Does NOT stop the dispatcher — it will re-read fresh config
    on its next poll cycle. Clears active_counts and pending queue.
    """
    st = _state()
    with st._lock:
        st._active_counts.clear()
        st._pending_queue.clear()


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
    st = _state()
    priority_order = _get_priority_order()
    active = get_active_counts()

    with st._lock:
        queue_size = len(st._pending_queue)

    provider_status = []
    for entry in priority_order:
        name = entry.get("name", "")
        provider_status.append({
            "name": name,
            "enabled": entry.get("enabled", True),
            "max_concurrent": entry.get("max_concurrent"),
            "active_count": active.get(name, 0),
            "excluded": _is_excluded_provider(name),
        })

    return {
        "enabled": _is_enabled(),
        "main_server": _get_main_server(),
        "exclude_from_subagents": _should_exclude_from_subagents(),
        "active_counts": active,
        "queue_size": queue_size,
        "queue_max": _get_queue_max_size(),
        "queue_enabled": _is_queue_enabled(),
        "dispatcher_running": st._dispatcher_running,
        "priority_order": provider_status,
        "goal_rules": _get_goal_rules(),
    }
