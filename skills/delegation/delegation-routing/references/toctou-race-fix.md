# TOCTOU Race Fix — Subagent Provider Acquisition

**Date:** 2026-07-10  
**Session context:** Parent (server 225) dispatched 4 subagents simultaneously; all returned "All N tasks were queued due to provider capacity limits."

## Problem

When 4+ subagents dispatch at once with `max_concurrent=1` per server, ALL get queued even though servers aren't truly busy. Previous tests (2-3 subagents) worked fine.

## Root Cause Analysis

### Initial Hypothesis (Wrong)
The `enhancements/fis-failures/` directory didn't exist when subagents dispatched.  
**Why it's wrong:** Previous tests created folders on the fly and worked fine.

### Actual Cause
All 4 subagents hit the priority_order routing (Path 3) simultaneously. Each checked a server, saw count=0 < max_concurrent=1, and reserved the slot. But:

- **Health check ran OUTSIDE the lock** — while one subagent was health-checking on server 224, another could see it as "busy" (count=1) even though no work was running yet
- By the time all health checks completed, other subagents had already committed to their reservations
- If a health check failed after reservation, the slot was released — but other subagents had already moved on

### Why Parent Doesn't Compete
- `exclude_from_subagents: true` prevents parent from being considered as a subagent provider
- Parent runs on server 225, separate from the four subagent servers (224, 222, 223, 221)

### Why Previous Tests Worked
- Typically dispatched 2-3 subagents at a time → at least one provider had capacity
- Goal rule matching (Path 2) caught some tasks without competing for Path 3 slots
- Fewer simultaneous dispatches meant less contention

## The Fix

**Move health check INSIDE the lock** in `acquire_provider()` (tools/subagent_router.py, lines 339–348).

### Before (TOCTOU race window)
```python
# Lock released here — race window opens
with _state_lock:
    current = _active_counts.get(name, 0)
    if current < int(max_conc):
        _active_counts[name] = current + 1
        reserved = True

# Health check runs OUTSIDE lock — another subagent can see "busy" slot
creds = _resolve_provider_credentials(name)
if creds and health_check(creds["base_url"]):
    return creds
```

### After (atomic reservation + validation)
```python
with _state_lock:
    current = _active_counts.get(name, 0)
    if current < int(max_conc):
        _active_counts[name] = current + 1
        reserved = True

if not reserved:
    continue

creds = _resolve_provider_credentials(name)
if creds:
    with _state_lock:  # Health check INSIDE lock
        if health_check(creds["base_url"]):
            return creds
        # Failed — release slot and try next server
```

## Debugging Pattern

When subagents appear "stuck" or all-queued:

1. **Check provider status** → `grep -i "subagent\|delegation\|acquired\|queued" ~/.hermes/logs/agent.log | tail -20`
2. **Verify directory exists** → `ls -la enhancements/fis-failures/` (but don't blame it if it doesn't)
3. **Don't loop on session_search** → if same result twice, change query or switch tools
4. **Check queue size** → `_pending_queue` in subagent_router.py should be < max_size (20)

## Key Takeaway

With `max_concurrent=1`, the bottleneck isn't server capacity — it's the **reservation/validation race**. The atomic fix ensures each subagent sees accurate slot status during health check, preventing the "all busy but none truly busy" scenario.
