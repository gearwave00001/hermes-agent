# Dispatcher Death During 6+ Parallel Subagents — Fix (July 10, 2026)

## Problem

When dispatching 6+ subagents in parallel, the dispatcher thread would die, requiring manual resubmission. The root cause was a cascade of 5 interrelated bugs.

## Bug 1: Nested Dispatch Cascade (PRIMARY CAUSE)

When `_dispatch_queued_unlocked()` failed to dispatch a task (e.g., `_dispatch_single_queued()` raised), it called `release_provider()` to free the reserved slot. But `release_provider()` checks if the dispatcher is alive and triggers queue dispatch — which spawns a new dispatch thread that re-dispatches the same remaining entries.

With 6 tasks, this created exponential thread explosion:
- Task 1 fails → release → dispatch thread A spawns
- Thread A tries task 2 → fails → release → dispatch thread B spawns
- Thread B tries task 3 → fails → release → dispatch thread C spawns
- ...until the process is overwhelmed

**Fix:** Added `skip_queue_dispatch=True` parameter to `release_provider()`. Inside `_dispatch_queued_unlocked()` error path, we call `release_provider(provider_name, skip_queue_dispatch=True)` which only decrements the counter without triggering queue dispatch or dispatcher restart.

## Bug 2: Config Cache Never Invalidates

`_get_queue_config()` cached the queue config on first call via `st._queue_config`. If config changed mid-session (e.g., via `/config` or `hermes config set`), the router used stale values.

**Fix:** Removed caching entirely. `_get_queue_config()` now re-reads from config on each call. The config loading is fast (dict merge) and called infrequently (only during enqueue/dispatch), so caching provides no meaningful benefit.

## Bug 3: `reset_state()` Killed Dispatcher Mid-Dispatch

`reset_state()` called `stop_dispatcher()` which set `_dispatcher_running = False`, killing the dispatcher thread while tasks were mid-dispatch. Config reloads or model switches that triggered `reset_state()` would orphan in-flight queued tasks.

**Fix:** `reset_state()` now only clears `active_counts` and `pending_queue`, leaving the dispatcher thread running. The dispatcher re-reads fresh config on its next poll cycle.

## Bug 4: Multiple Dispatcher Threads Racing

Two threads could both see the dispatcher as dead and each spawn a new one. The check-then-start race in `_ensure_dispatcher()`:
1. Thread A checks → dispatcher dead
2. Thread B checks → dispatcher dead (A hasn't started new thread yet)
3. Both spawn new dispatcher threads → two concurrent dispatchers

**Fix:** Added `_thread_id` counter (monotonically increasing integer) that tracks which dispatcher thread is "ours". `_ensure_dispatcher()` checks the ID before starting a new thread. The dispatcher loop checks `_thread_id` on each iteration and exits if replaced. Critical ordering: `_thread_id` is set BEFORE `thread.start()` so the new thread sees its own ID on the first loop iteration.

## Bug 5: Stale Daemon Threads

`stop_dispatcher()` set `_dispatcher_running = False` but didn't wait for the thread to actually exit. Old daemon threads would continue running, checking stale state, and racing with newly started dispatchers (especially after `force_reset()` in tests).

**Fix:** `stop_dispatcher()` now joins the thread (5s timeout) before returning.

## Files Changed

- `tools/subagent_router.py` — all 5 fixes
- `tests/tools/test_subagent_router_deadlock.py` — 6 new tests covering all failure modes
