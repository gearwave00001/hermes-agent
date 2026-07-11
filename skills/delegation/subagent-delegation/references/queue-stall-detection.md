# Queue Stall Detection — Root Cause Analysis and Fixes

## Symptom

When dispatching 8-10+ subagents simultaneously, some queue due to capacity limits.
Normally they start within 60-90 seconds as servers go idle, but can remain stuck
for 3+ minutes even with all servers healthy. The dispatcher thread dies
(`_dispatcher_running: False`) while tasks sit in the queue — queued tasks are
permanently orphaned even after all servers go idle.

## Root Causes (Fixed Jul 2026)

Six interconnected bugs combined to produce the dead orchestrator:

### BUG 1 (Primary): Health check ran INSIDE the global lock

`acquire_provider()` held `st._lock` during the HTTP health check (up to 3s per
provider). Since ALL provider acquisition, release, AND queue dispatch share this
lock, a single health check blocked every other operation for its duration. With
4 providers at max_concurrent=1, worst case was 12s of lock holding per dispatch
cycle — queued tasks starved because `release_provider()` couldn't decrement
`active_counts` while a health check held the lock.

**Fix**: Health check now runs OUTSIDE the lock. The reserve-first pattern
(reserve slot → health check → proceed) already prevents TOCTOU — the slot is
reserved before the health check, and released on failure.

### BUG 2: `_dispatcher_loop` didn't check `_dispatching` flag

The dispatcher loop snapshot+cleared the queue without checking if
`_try_dispatch_queued()` (called from `release_provider`) was already dispatching
those same entries. Both paths could grab the same queue entries, causing duplicate
dispatch and corrupting `active_counts`.

**Fix**: Added `_dispatching` check inside the lock before snapshotting, with
proper finally-block cleanup that always clears the flag even on exception.

### BUG 3: `_dispatching` flag not cleared on exception

If `_dispatch_queued_unlocked` raised an exception, the `_dispatching` flag stayed
True forever, permanently blocking the dispatcher loop from ever dispatching again.

**Fix**: try/finally in the dispatcher loop always clears `_dispatching`.

### BUG 4: Wrong provider released on dispatch failure

When `_dispatch_single_queued` failed, the error handler guessed which provider to
release by iterating `get_active_counts()` and picking the first with count > 0.
With multiple providers active, this released the wrong one, creating phantom
capacity that blocked future dispatches.

**Fix**: Now uses `entry["creds"]["provider"]` (set when `acquire_provider`
succeeded) with a base_url-based fallback.

### BUG 5: Sync wait timeout was 600s (10 minutes)

When all tasks were queued and the dispatcher died, the parent blocked for 10
minutes before timing out — far too long for a stuck dispatcher.

**Fix**: Reduced to 120s (2 minutes).

### BUG 6: Missing call-path documentation

`_dispatch_queued_unlocked` is called from 4 different paths (dispatcher loop,
`_try_dispatch_queued`, `_ensure_dispatcher` inline, `release_provider` inline).
Without documentation of how they interact, future changes easily reintroduce races.

**Fix**: Added documentation of all 4 call paths and their interaction patterns.

## Detection Script

```python
from tools.subagent_router import _dispatcher_running, _state, get_active_counts

st = _state()
print(f'Queue size: {len(st._pending_queue)}')
print(f'Dispatcher running: {_dispatcher_running}')
print(f'Dispatching flag: {st._dispatching}')
counts = get_active_counts()
print(f'Active counts: {counts}')
```

## Symptoms
- Subagent completion banners never arrive
- `process(action='list')` shows no visible processes (delegation system manages independently)
- Server health checks show all servers running normally
- Queue may be empty or have stale entries
- Dispatcher thread is dead (`_dispatcher_running: False`)
- `_dispatching` flag stuck at True with no active dispatch

## Fix
Re-dispatch stuck subagents fresh with a new `delegate_task()` call. This creates new delegation IDs and starts them immediately rather than waiting for the dead queue dispatcher. The old queued tasks eventually complete too but their results are already in — no harm in re-dispatching.

## Prevention Tips
- When dispatching large fleets (8+), monitor for completion within 4 minutes
- If queued subagents haven't started after 4 minutes despite all servers idle, re-dispatch
- The queue is not critical — orphaned tasks still complete eventually; re-dispatching is safe

## Reproduction Test

`tests/tools/test_subagent_router_deadlock.py` contains 7 tests that reproduce
each bug scenario. Run with:
```bash
python3 -m pytest tests/tools/test_subagent_router_deadlock.py -v
```

## Key Files

- `tools/subagent_router.py` — provider routing, queue management, dispatcher loop
- `tools/delegate_tool.py` — `delegate_task()` entry point, child build/run, sync wait
- `tools/async_delegation.py` — async dispatch to daemon executor
