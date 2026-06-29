# GPU Server Routing — Edge Cases & Failure Modes

## Queue-Full vs At-Capacity: Two Different Failure Modes

These are **not the same thing** and produce different return values:

| Mode | Where it fails | Return value | Meaning |
|------|----------------|--------------|---------|
| **Queue full** | `enqueue_task()` — queue hit `_queue_config.max_size` (default 20) | `False` | Task was rejected immediately; never entered the queue. Caller can retry or reject. |
| **At capacity** | `acquire_provider()` — all servers in `priority_order` have `_active_counts >= max_concurrent` | `None` | Server slot is full; task falls through to `delegation.provider` or queues depending on config. |

**Why this matters:** A caller that checks the return value of `enqueue_task()` can distinguish "queue is saturated" from "server is busy." This affects whether the parent should retry, wait, or route elsewhere.

## Goal Rule Fallback When Target Is Full

When a goal rule matches and points to server 224 but 224 is at `max_concurrent`:

1. The router **does not queue on 224** — it falls back to the next server in `priority_order`.
2. This happens because `_match_goal_rule()` returns the provider name, then `acquire_provider()` tries to acquire it. If the capacity check fails (`_active_counts[name] >= max_concurrent`), it continues walking `priority_order` rather than holding on the goal-matched server.
3. **Ambiguity:** If the goal rule is a hard requirement (e.g., "code review" must go to 224), the fallback may not be ideal. The current implementation treats goal rules as soft preferences — they match first, but capacity still applies.

**To force a goal-rule server even at capacity**, set its `max_concurrent` higher than your typical load, or use per-task provider override (`tasks[].provider`) to bypass the router entirely.

## Release-Triggered Queue Dispatch Timing

When a subagent completes and calls `release_provider()`:

1. The counter decrements (`_active_counts[name] -= 1`).
2. If `_pending_queue` is non-empty, `_try_dispatch_queued()` fires.
3. This spawns a **background thread** (`_dispatch_queued_unlocked`) that re-runs `acquire_provider()` for each queued entry.
4. Successfully dispatched entries get their providers acquired and are sent to child agents. Failed entries go back into the queue.

**Timing pitfall:** The background thread runs independently, so there's a window where:
- A new `acquire_provider()` call from the parent could grab the slot that just opened.
- A queued task might see the server as "at capacity" briefly even though a slot just freed up.

This is usually harmless (the queue dispatcher retries on the next poll interval), but in high-throughput scenarios it can cause one extra dispatch cycle.

## Provider Resolution Failure Cascade

When `_resolve_provider_credentials()` fails for the top-priority server:

1. The router **does not return None immediately** — it tries the next server in `priority_order`.
2. Common failure causes: missing API key, wrong credential format, or the provider name doesn't exist in `custom_providers`.
3. If ALL servers fail resolution, the router returns `None` and falls through to `delegation.provider`.

**Verification:** Check that `_resolve_provider_credentials()` is called once per server in priority order, not cached across calls. Each call resolves fresh credentials.

## Idempotent Release (Zero Count Edge Case)

Calling `release_provider("server")` when the server's active count is already 0:

- Logs a warning: `"release_provider called for 'server' with active_count=0"`
- Does **not** crash or go negative.
- Safe to call repeatedly (idempotent).

This can happen if a subagent reports completion but the release was already triggered by another path (e.g., timeout cleanup, or a manual release before the subagent finished).

## Mixed Enabled/Disabled Servers Under Load

When some servers are `enabled: false` in config:

1. The router **skips them entirely** during `acquire_provider()` — they don't count toward capacity.
2. Even if a disabled server has available slots (`_active_counts < max_concurrent`), new subagents won't route to it.
3. **Important:** A server can be at capacity (enabled) while another is disabled with free slots. New subagents will fill the enabled servers first, then skip disabled ones.

To temporarily "reserve" a server without disabling it, set its `max_concurrent` to 0 (effectively zeroing out its capacity).

## Sync Queuing Behavior

With `sync_event` set on an enqueued task:

1. The parent thread blocks on the event until the queued task completes.
2. The dispatcher sets the event after the child agent finishes and releases its provider.
3. **Key invariant:** The parent does NOT proceed while tasks are still executing in the queue — even if a slot has opened up, the sync-waiting task holds the parent's attention.

## Health Check During Queue Dispatch

The health check is called **twice** for queued tasks:

1. **At acquire time** (before incrementing `_active_counts`) — verifies the server is alive.
2. **During queue dispatch** (`_dispatch_queued_unlocked`) — if a server goes down between enqueue and dispatch, the dispatcher skips it and tries the next server.

This double-check prevents routing to a server that was healthy when enqueued but died before dispatch.
