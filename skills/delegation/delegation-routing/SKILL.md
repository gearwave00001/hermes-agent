---
name: delegation-routing
description: "Router: provider acquisition, queue, dispatcher bugs."
version: 2.0.0
author: Hermes Agent
tags: [delegation, routing, servers, subagents, provider, queue, dispatcher]
---

# Delegation Routing

Covers the internals of `tools/subagent_router.py` — provider acquisition, queue management, dead dispatcher bugs, and the completion drain path.

For dispatch best practices, tracking, and common pitfalls, see the `subagent-delegation` skill.

## Core Concepts

- **subagent_routing config block** in `~/.hermes/config.yaml` controls routing behavior
- **priority_order**: ordered list of servers with `enabled`, `max_concurrent`, and `purpose` fields
- **main_server** is excluded from subagent pool when `exclude_from_subagents: true` — the router checks `_is_excluded_provider()` on all 3 acquisition paths (override, goal rule, priority order)
- **acquire_provider()** resolves credentials per-child and assigns to the first available server

## main_server Sync on Model Switch

When the user runs `/model <name> --global` and switches to a named custom provider (e.g., `custom:192.168.1.225`), Hermes automatically updates TWO config values alongside `model.default` and `model.provider`:

- **`model.base_url`** — set to the resolved base URL of the new provider (or cleared if switching away from a custom endpoint)
- **`subagent_routing.main_server`** — set to the server name extracted from the provider slug (e.g., `192.168.1.225`), or cleared when switching to a non-custom provider

This ensures the main conversation's server stays in sync with subagent routing, so `exclude_from_subagents: true` continues to exclude the correct server after a model switch. The sync function `_update_main_server_on_switch()` lives in `hermes_cli/model_switch.py` and is called from both the CLI path (`_apply_model_switch_result`) and the TUI/gateway path (`_persist_model_switch`).

### CRITICAL: delegation.provider does NOT auto-sync

`delegation.provider` is NOT touched by `_update_main_server_on_switch()`. It retains whatever value was set before the model switch. This creates a direct conflict:

1. User switches main conversation to server 224 via `/model`
2. `subagent_routing.main_server` syncs to `192.168.1.224`
3. `exclude_from_subagents: true` now excludes 224
4. `delegation.provider` still points to `192.168.1.224` (the old value)
5. Subagents try to acquire the excluded server and fail to route properly

**Fix after a model switch:** Verify `delegation.provider` points to a non-excluded server:
```bash
hermes config set delegation.provider "192.168.1.222"  # or another active subagent target
```

**Rule of thumb:** After any `/model` switch that changes the main conversation's server, always check that `delegation.provider` does NOT equal `subagent_routing.main_server` when `exclude_from_subagents` is true. If they match, subagents are targeting the excluded server.

## Config Reference

```yaml
subagent_routing:
  enabled: true
  health_check_timeout: 3
  queue:
    enabled: true
    max_size: 20
    poll_interval: 2
  priority_order:
    - name: 192.168.1.224
      purpose: heavy workloads, long context
      enabled: true
      max_concurrent: 1
    - name: 192.168.1.222
      purpose: compute-heavy tasks (ComfyUI/gaming idle)
      enabled: true
      max_concurrent: 1
  main_server: 192.168.1.225
  exclude_from_subagents: true
```

## Server Pool

| Server | IP | GPU | Model | Subagent Target? |
|--------|----|-----|-------|------------------|
| 224 | 192.168.1.224 | 2x R9700 PRO AI 32GB (64GB VRAM) | Qwen3.6-27B-FP8 | NO (main_server, excluded) |
| 225 | 192.168.1.225 | RTX 5060 Ti 16GB | Huihui-Qwen3.6-35B-A3B-abliterated-ggml-model-Q6_K.gguf | YES (priority: 5th) |
| 222 | 192.168.1.222 | RTX 5090 32GB | Huihui-Qwen3.6-27B-abliterated-ggml-model-Q6_K.gguf | YES (priority: 2nd) |
| 223 | 192.168.1.223 | RTX 5080 | Huihui-Qwen3.6-35B-A3B-abliterated-ggml-model-Q6_K.gguf | YES (priority: 3rd) |
| 221 | 192.168.1.221 | RX 7900 XTX | Huihui-Qwen3.6-35B-A3B-abliterated-ggml-model-Q6_K.gguf | YES (priority: 4th) |

**NOTE:** Server 224 is currently the main_server (as of Jul 11, 2026 model switch). This table reflects the active config. When main_server changes, update this table AND verify `delegation.provider` does not point to the excluded server.

## Provider Acquisition Flow

`acquire_provider()` uses three paths in priority order:

1. **provider_override** — explicit per-task override (highest priority)
2. **goal_rules match** — config-based rule matching by goal substring
3. **priority_order routing** — first available server from the ordered list

## Common Pitfall: TOCTOU Race Condition

When dispatching multiple subagents in the same batch, they can both land on the **same server** even when different servers are available.

### Root Cause

`acquire_provider()` in `tools/subagent_router.py` had a check-then-increment race (TOCTOU):

```python
with _state_lock:
    current = _active_counts.get(name, 0)   # CHECK (lock released after this block)
    if current >= int(max_conc):
        continue
# Health check runs OUTSIDE the lock here — up to 3 seconds
with _state_lock:
    _active_counts[name] = _active_counts.get(name, 0) + 1  # ACQUIRE
```

### Fix

**Move the health check INSIDE the lock** in `acquire_provider()`. The sequence becomes atomic:

1. Lock → check count < max_concurrent → increment count → unlock
2. If reserved, lock again → do health check → if pass, return creds; if fail, decrement count and release slot

**Verified fix**: The health check now runs inside `_state_lock` so reservation + validation are atomic. See `references/toctou-race-fix.md` for the full debugging session.

## Queue Management

- Overflow queue with background dispatcher thread
- Tasks enqueue when all providers are at capacity
- Dispatcher polls every `poll_interval` seconds (default: 2s)
- Max queue size: configurable via `queue.max_size` (default: 20)
- Sync queuing supported — parent blocks until all queued tasks complete

## Common Pitfall: Dead Dispatcher — UnboundLocalError (Root Cause)

**The committed `_dispatcher_loop()` had an `UnboundLocalError` that crashed every poll cycle when the queue was empty (the normal state).**

### The Bug

```python
# BUGGY:
while _dispatcher_running:
    try:
        with _state_lock:
            if not _pending_queue:
                pass  # <-- tasks_snapshot NEVER assigned on this path
            else:
                tasks_snapshot = list(_pending_queue)  # only assigned here
        if tasks_snapshot:  # <-- UnboundLocalError!
            _dispatch_queued_unlocked(tasks_snapshot)
```

Python marks `tasks_snapshot` as local, but when the queue is empty, the assignment never executes. Result: `UnboundLocalError` every poll cycle.

### Fix (Applied)

Initialize `tasks_snapshot` BEFORE the try block:
```python
tasks_snapshot: list[dict] = []  # always initialized
```

**CRITICAL: After fixing the workspace code, you must build and install the wheel into the sandbox, then restart the Hermes process.** The running Hermes process loads from `~/.hermes/hermes-agent/venv/`, NOT the workspace directory.

## Common Pitfall: Dead Dispatcher After Module Reimport

A model switch or config reload can trigger a module reimport of `tools/subagent_router.py`, which resets module-level globals (`_dispatcher_running = False`, `_pending_queue = []`), killing the dispatcher thread and wiping queued tasks.

### Fix (Already Applied)

Uses a `_RouterState` singleton whose `_instance` is stored in `sys.modules` under a stable key, so reimport recovers the same live state object. Additionally:

- `release_provider()` now checks dispatcher liveness and dispatches inline if dead
- `_ensure_dispatcher()` auto-restarts a dead dispatcher and dispatches pending tasks inline

Verify `tools/subagent_router.py` contains the `_RouterState` class and `_state()` function (not plain module-level globals).

## Common Pitfall: Dispatcher Death During 6+ Parallel Subagents

When dispatching 6+ subagents in parallel, the dispatcher thread can die from a cascade of nested dispatch threads.

### Five Bugs Fixed (July 10, 2026)

1. **Nested dispatch cascade** — `release_provider()` was called from `_dispatch_queued_unlocked()` error path, triggering queue dispatch that re-dispatched the same remaining entries. **Fix:** `release_provider(provider_name, skip_queue_dispatch=True)` only decrements the counter without triggering dispatch.
2. **Config cache never invalidates** — `_get_queue_config()` cached on first call. **Fix:** now re-reads config on each call.
3. **`reset_state()` killed dispatcher mid-dispatch** — called `stop_dispatcher()` which stopped the thread while tasks were mid-dispatch. **Fix:** `reset_state()` only clears `active_counts` and `pending_queue`, leaving the dispatcher running.
4. **Multiple dispatcher threads racing** — two threads could both see the dispatcher as dead and each spawn a new one. **Fix:** `_thread_id` counter (monotonically increasing) tracks which dispatcher is "ours".
5. **Stale daemon threads** — `stop_dispatcher()` didn't wait for thread to actually exit. **Fix:** now joins thread (5s timeout).

See `references/dispatcher-death-6-parallel-fix.md` for the full debugging session.

## Common Pitfall: Dead Dispatcher After Fleet Completion

After all subagents in a fleet complete and release their slots, the dispatcher thread can die. New tasks don't restart it, so any tasks that get enqueued sit with no one to dispatch them — even though all servers show as idle.

### Fix

Add `_ensure_dispatcher()` call in `acquire_provider()` so that every provider acquisition attempt also ensures the dispatcher is alive.

**Verification:**
```bash
grep "Started subagent router dispatcher thread" ~/.hermes/logs/agent.log | tail -1
```



## Common Pitfall: main_server Exclusion Not Enforced (July 11, 2026)

The `exclude_from_subagents: true` config flag existed in `config.yaml` but was **never read by the router**. `subagent_router.py` iterated all `priority_order` entries unconditionally, so the main_server received subagent work despite being marked excluded.

### Symptoms

Subagents dispatched to the main_server even when `exclude_from_subagents: true`. The main_server's `max_concurrent` slot gets consumed by subagent work, potentially starving the parent agent.

### Fix Applied

Three new helpers in `tools/subagent_router.py`:
- `_get_main_server()` — reads `main_server` from config
- `_should_exclude_from_subagents()` — reads `exclude_from_subagents` flag
- `_is_excluded_provider(name)` — returns True if that provider should be skipped

Exclusion checks added to all 3 acquisition paths:
- Path 1 (explicit override) — skips if excluded
- Path 2 (goal rule match) — skips if excluded
- Path 3 (priority order routing) — skips before capacity check

`get_status()` also updated to show `excluded` flag per provider and `main_server`/`exclude_from_subagents` at top level.

### Verification

```bash
bash scripts/run_tests.sh tests/tools/test_subagent_router.py --tb=short
bash scripts/run_tests.sh tests/tools/test_subagent_router_deadlock.py --tb=short
```

Both suites pass (33 + 13 = 46 tests).



## Common Pitfall: All-Queued Saturation (4+ simultaneous dispatches)

When 4+ subagents dispatch simultaneously and all providers are at capacity, ALL tasks get queued. Their results eventually re-enter via the dispatcher — this is **expected behavior**, not a bug.

Detection: `"All N tasks were queued due to provider capacity limits."` in dispatch output. The dispatcher picks them up within 2–4 minutes.

### Mixed-Model Fleet Dispatch (10 Subagents — Validated Pattern)

Validated across two separate fleet tests (July 10 and July 11, 2026):
- First 4 dispatched immediately on their assigned servers (221–224) — confirmed no TOCTOU collision
- Remaining 6 queued due to per-server `max_concurrent` limits — picked up when any server had room
- Mixed models across servers coexist normally
- Durations ranged from ~95s to ~264s



## Common Pitfall: toolsets Parameter Ignored at Runtime

The `toolsets` parameter in `delegate_task()` is declared in the schema but never actually wired through to child agents. Children always inherit all parent tools regardless of what the model specifies.

### Root Cause

Three locations in `tools/delegate_tool.py` hardcode `toolsets=None` in `_build_child_agent()` and `dispatch_async_delegation_batch()`.

### Current Status

**PR #61082** ("fix(delegate): narrow leaf child toolsets" by HOYALIM, opened Jul 8 2026) addresses this:
- Narrows default leaf subagent tool exposure (terminal + file + web, intersected with parent)
- Prunes blocked tools from schemas for leaf children
- Reduces schema size by ~71% (43k → 12.5k chars)

### Workaround Until PR Lands

Use the `tasks` batch form with explicit per-task toolsets and document it in your goal text:

```python
delegate_task(
    tasks=[{
        "goal": "Only use file tools. Do NOT call terminal.",
        "context": "...",
        "toolsets": ["file"],
    }]
)
```

## Common Pitfall: Premature Summarization

Don't give a final summary while subagents are still running. Wait for the `[ASYNC DELEGATION BATCH COMPLETE]` messages before reporting results.

## Common Pitfall: Transient Server Connection Refused

A server that successfully served an earlier subagent can become temporarily unavailable for a later queued task. The dispatcher does NOT auto-retry — it reports the failure and moves on.

### Symptom

```
API call failed after 3 retries: HTTP 500: dial tcp4 192.168.1.221:5678: connect: connection refused
```

### Fix

Re-dispatch the failed task. The dispatcher will assign it to whatever server has capacity:

```python
delegate_task(goal="same goal", context="same context")
```

### Prevention

Not preventable — servers can go down between dispatch and execution (queued tasks may wait minutes). Monitor for `connection refused` in async delegation results and re-dispatch any failures.

## Completion Drain Path — How Subagent Results Re-enter the CLI

The chain from subagent completion to CLI delivery has 3 steps, each with a silent failure path.

### The Chain

1. **`_push_completion_event()`** (`tools/async_delegation.py`) — Pushes `type="async_delegation"` event onto `process_registry.completion_queue` (in-memory `queue.Queue`)
2. **`drain_notifications()`** (`tools/process_registry.py`) — Pops events from queue, formats them via `format_process_notification()`. Skips `type="completion"` events already consumed via `wait()`/`log()`/`poll()`. Does NOT skip `async_delegation` events.
3. **CLI drain sites** (`cli.py` process_loop) — Two locations call `drain_notifications()` and put results into `_pending_input`:
   - **Idle loop** (~line 15083): Runs every 0.1s when agent is NOT running
   - **Post-turn** (~line 15252): Runs after `self.chat()` returns

### Known Failure Modes

| Failure | Cause | Symptom |
|---------|-------|---------|
| CLI restart before drain | `completion_queue` is in-memory | Events enqueued but never drained |
| Drain `except Exception: pass` | Any exception in drain silently swallowed | Events sit in queue forever (now logged as WARNING) |
| `_drain_should_skip()` overreach | Could skip async_delegation if `type` check removed | Completions silently dropped |
| `format_process_notification()` returns None | Missing required fields in event | Event popped but not queued into `_pending_input` (now logged as WARNING) |

### Logging (Added July 11, 2026)

- **Enqueue**: `Async delegation deleg_X: completion event enqueued (status=..., session_key=...)`
- **Drain**: `drain_notifications: drained N events (X formatted, Y skipped, Z dropped)`
- **CLI idle**: `process_loop (idle): drained N process notifications, queuing into _pending_input`
- **CLI post-turn**: `process_loop (post-turn): drained N process notifications, queuing into _pending_input`

### Debugging Missing Completions

```bash
# 1. Check if completion was enqueued
grep "completion event enqueued" ~/.hermes/logs/agent.log | tail -5
# 2. Check if drain ran
grep "drain_notifications:" ~/.hermes/logs/agent.log | tail -10
# 3. Check if CLI queued into pending_input
grep "process_loop.*drained.*process notifications" ~/.hermes/logs/agent.log | tail -5
```

### Key Invariant

`_drain_should_skip()` only applies to `type="completion"` events. `async_delegation` and `watch_match` events are NEVER skipped.

### Tests

- `test_drain_notifications_does_not_skip_async_delegation_events`
- `test_drain_notifications_skips_only_type_completion`
- `test_drain_notifications_logs_skipped_and_dropped`
- `test_async_delegation_completion_queue_survives_drain`
- `test_completion_queue_is_in_memory_only`

See `references/completion-drain-path.md` for the full debugging session.

## Related Skills

- `subagent-delegation` — Dispatch best practices, tracking, monitoring, common pitfalls
- `parallel-research-delegation` — Multi-subagent research workflow with compiled reports

## Reference Files

- `references/dispatcher-death-6-parallel-fix.md` — 5 bugs causing dispatcher death during 6+ parallel subagents (July 10, 2026)
- `references/toctou-race-fix.md` — TOCTOU race condition in provider acquisition
- `references/completion-drain-path.md` — Completion drain chain debugging
