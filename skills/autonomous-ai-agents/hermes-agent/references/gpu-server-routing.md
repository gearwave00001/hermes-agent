# GPU Server Routing for Subagents

## Overview

When the main agent runs on a single-request-per-connection inference server,
subagents route to separate HTTP connections on other servers. Each subagent
gets its own slot — they don't contend with the parent's inference turn.

## Config Structure

All GPU servers are declared under `custom_providers` in `~/.hermes/config.yaml`.
The main conversation uses `model.base_url`; subagents pick from
`custom_providers` based on the `model` field passed to `delegate_task`.

### Server Annotation Pattern

Each server entry includes:
- **name** — used as a provider identifier when routing (`"192.168.1.224"`)
- **base_url** — must include port even if standard (5678)
- **api_key** — typically `proxy-managed` for local clusters
- **model** — the default model on this server
- **models** (optional) — nested models with context_length overrides
- **Comment lines** — describe GPU, capacity, power characteristics

### Enabling / Disabling Servers

To disable a server without deleting it:
```yaml
# - name: 192.168.1.221          # Commented out — subagents skip this server
#   base_url: http://192.168.1.221:5678/v1/
#   api_key: proxy-managed
#   model: Huihui-Qwen3.6-35B-A3B-abliterated-ggml-model-Q6_K.gguf
```

To set a **default** for all subagents (instead of picking per-task):
```yaml
delegation:
  provider: "192.168.1.224"     # All subagents go to this server by default
```

## Port Conventions

| Server | Inference | Port | Notes |
|--------|-----------|------|-------|
| 225 | llama.cpp | 5678 | Primary, always-on, power efficient |
| 221 | llama.cpp | 5678 | RX 7900 XTX 24GB, power inefficient |
| 222 | llama.cpp | 5678 | RTX 5090 32GB, largest single GPU |
| 223 | llama.cpp | 5678 | RTX 5080 16GB, balanced |
| 224 | vLLM      | 5678 | 2x R9700 PRO AI (64GB VRAM), highest context |

Note: vLLM can be overridden to use port 5678 like the others. Check with
`vllm serve --help` or the deployment config if unsure.

## Routing Decision Pattern

When I call `delegate_task`, I choose the server based on task characteristics:

- **Heavy / long-context** → 224 (highest VRAM, context window)
- **Compute-heavy** → 222 (RTX 5090, largest single GPU)
- **Balanced** → 223 (RTX 5080, good speed/power ratio)
- **Power-conscious** → 225 (if available, or 221 if power doesn't matter)
- **Default fallback** → first server in `custom_providers` list

To explicitly route to a specific server:
```python
delegate_task(
    goal="...",
    context="...",
    model={"provider": "192.168.1.224", "model": "Qwen3.6-27B-FP8"}
)
```

## vLLM vs llama.cpp on Same Port

vLLM and llama.cpp can coexist on the same port (5678). The difference is in
how they handle concurrency:
- **llama.cpp** — single request per connection; subagents get separate connections
- **vLLM** — handles multiple concurrent requests natively via continuous batching

If a server is running vLLM but overridden to use port 5678, the `base_url`
still uses `5678/v1` (OpenAI-compatible endpoint). The model name in the config
indicates which inference backend is expected.

## Maintenance Checklist

When managing servers over time:
1. **Check port** — confirm each server's port matches its entry in config
2. **Verify connectivity** — `curl http://<ip>:5678/v1/models` should return model info
3. **Comment out reserved servers** — if dedicating a server to another purpose
4. **Update model names** — when swapping models on a server, update the `model` field
5. **Check VRAM availability** — high-VRAM servers (224) may have reduced capacity
   when running other workloads simultaneously

## Debugging: Subagent Not Routing to Expected Server

If you spawn a subagent and it doesn't seem to be using the target server:

1. **Check `delegation.provider`** — if empty (`''`), subagents inherit from parent (225).
   This is the #1 cause of "subagent spawned but stayed on 225."
   ```bash
   grep -A 3 "^delegation:" ~/.hermes/config.yaml
   # Look for: provider: ''  ← empty means inherit from parent
   #            provider: 192.168.1.224  ← set, will route to 224
   ```

2. **Check `delegation.model`** — if the model name doesn't match the target server's
   expected model, the subagent may use the wrong model even on the right server.

3. **Verify custom_providers** — confirm the target server is defined with a valid
   `base_url`:
   ```bash
   grep -A 5 "192.168.1.224" ~/.hermes/config.yaml
   ```

4. **Set and test** — set both fields explicitly, then spawn a test subagent:
   ```bash
   hermes config set delegation.provider "192.168.1.224"
   hermes config set delegation.model "Qwen3.6-27B-FP8"
   # Then delegate_task — it should now route to 224 with the correct model.
   ```

5. **Verify the result** — check the subagent's summary for `model: Qwen3.6-27B-FP8`
   (not the parent's model) and `provider: 192.168.1.224`.

6. **If the subagent hangs** — see "Subagent Provider Hangs" in
   `references/multi-server-delegation.md` for the three-layer defense
   (pre-flight health check, `child_timeout_seconds`, stale-stream timeout).

## The subagent_routing Block

Hermes also defines a `subagent_routing` section in config.yaml that provides
a richer routing strategy beyond the single `delegation.provider`:

```yaml
subagent_routing:
  priority_order:
  - name: 192.168.1.224
    purpose: heavy workloads, long context
    enabled: true
    checked_out: false
  - name: 192.168.1.222
    purpose: compute-heavy tasks (ComfyUI/gaming idle)
    enabled: true       # was false; enable when ComfyUI is idle
    checked_out: false
  - name: 192.168.1.223
    purpose: general subagent work
    enabled: true       # was false
    checked_out: false
  - name: 192.168.1.221
    purpose: offload when watts don't matter
    enabled: true
    checked_out: false
  main_server: Server_llamacpp
  exclude_from_subagents: true
```

**Key fields:**
- `priority_order` — list of servers tried in sequence for subagent routing
- `enabled` — whether the server is available (set to `false` when dedicated to
  another purpose like ComfyUI or gaming)
- `checked_out` — legacy config field; the actual concurrency tracking uses an
  in-memory `_active_counts` dict protected by a lock. A server is "in use" when
  `_active_counts[name] >= max_concurrent`. The `checked_out` flag in config is
  informational only and not read at runtime by the router.
- `max_concurrent` — REQUIRED. Maximum simultaneous subagents on this server.
  If missing, the server is skipped entirely.

**Important distinction:** When `subagent_routing.enabled: false` (default),
`delegate_task` reads `delegation.provider` directly — all subagents go to one
server. When `subagent_routing.enabled: true`, each child calls
`acquire_provider()` independently, which walks `priority_order` top-to-bottom
and assigns the first available server based on `_active_counts` vs `max_concurrent`.
This means subagents can be distributed across different servers even though
`delegation.provider` is set to a single value.

To use per-child priority routing:
```yaml
subagent_routing:
  enabled: true          # ← must be true for per-child routing
  priority_order:
    - name: 192.168.1.224
      max_concurrent: 1  # ← REQUIRED — skip if missing
      enabled: true
```

To use the legacy single-provider mode (all subagents to one server):
```yaml
subagent_routing:
  enabled: false         # or omit entirely
delegation:
  provider: "192.168.1.224"  # ← all children route here
```

## Fallback Providers for Delegation (Feature #7481)

**Current behavior — verified via source code (2026-06-20):** Subagents **do**
inherit the parent's `_fallback_chain` at runtime. In `delegate_tool.py` line 1187:

```python
parent_fallback = getattr(parent_agent, "_fallback_chain", None) or None
# passed to child as fallback_model=parent_fallback in AIAgent() call
```

The real limitation is that the **top-level** `fallback_providers: []` in config.yaml
is empty by default. So while the inheritance mechanism works, the chain may be thin
(just the parent's own provider) unless we populate it.

**What this means for our LAN setup:**
- When 224 goes down, the subagent falls back to whatever is in the parent's
  `_fallback_chain` — currently just `Server_llamacpp` (225).
- To get multi-server fallback, we need either:
  1. Populate the top-level `fallback_providers: []` with LAN server entries
  2. Add a new `delegation.fallback_providers` field (forward-compatible; not yet
     explicitly consumed by delegation code but harmless to add)

**Current behavior:**
- `config.yaml` supports `fallback_providers: []` for the main agent
- `delegation:` section supports only a single `model` + `provider` pair
- When a subagent's primary provider fails, fallback goes through `_fallback_chain`

**Proposed solution** (feature request #7481): Allow `delegation:` to accept its
own fallback chain:

```yaml
delegation:
  model: "Qwen3.6-27B-FP8"
  provider: "192.168.1.224"
  fallback_providers:
    - name: 192.168.1.224
      model: Qwen3.6-27B-FP8
    - name: 192.168.1.221
      model: Huihui-Qwen3.6-35B-A3B-abliterated-ggml-model-Q6_K.gguf
```

**Workaround today:** Enable servers in `subagent_routing.priority_order` and
set `delegation.provider` to the highest-priority server. When that server is
unavailable, manually specify a different provider via the `model` parameter:

```python
delegate_task(
    goal="...",
    context="...",
    model={"provider": "192.168.1.222", "model": "Huihui-Qwen3.6-..."}
)
```

**Alternative workaround:** Have subagents inherit the parent's `fallback_providers`
by default (with an opt-out flag). This is the approach proposed in #7481.

## Test Pattern: Proving One Server Works Before Scaling

Before routing to multiple servers, prove one works end-to-end:

1. Set `delegation.provider` to a single server
2. Spawn a subagent with a clear goal and context about the target server
3. Verify the result includes the correct model name
4. Create a verification file on disk to confirm the subagent completed
5. If it works, the routing layer is functional — add more servers

This pattern was used to confirm 224 works: set provider + model, spawned a
test subagent that created `test_224_result.txt`, and verified the result
showed `model: Qwen3.6-27B-FP8` with `exit_reason: completed`.

## Source-Code Verification (2026-06-20)

When tracing the actual code path for subagent routing, we confirmed:

1. **`_resolve_delegation_credentials()`** reads `delegation.provider` from config
   and calls `resolve_runtime_provider()` which looks up the name in `custom_providers`.
   This returns the full credential bundle (base_url, api_key, model).

2. **Subagents inherit `_fallback_chain`** — at line 1187 of `delegate_tool.py`:
   ```python
   parent_fallback = getattr(parent_agent, "_fallback_chain", None) or None
   # passed to child AIAgent as fallback_model=parent_fallback
   ```

3. **Two config systems serve overlapping purposes:**
   - `subagent_routing.priority_order` — priority chain with enabled/max_concurrent flags.
     When `subagent_routing.enabled: true`, this IS directly read by `delegate_task` via
     the per-child `acquire_provider()` call which walks the list top-to-bottom and assigns
     the first server where `_active_counts[name] < max_concurrent`.
   - `custom_providers` — full credential bundles for each server (fully consumed by
     the runtime provider resolution path). These are what `acquire_provider()` resolves
     via `resolve_runtime_provider()`.

4. **The proposed `delegation.fallback_providers` field** sits in config but isn't
   explicitly consumed by delegation code yet. Adding it is safe and forward-compatible.

5. **Runtime provider lookup** — when `delegation.provider: 192.168.1.224`, the
   system calls `_resolve_named_custom_runtime()` which finds the entry in
   `custom_providers` and returns its base_url, api_key, and model. This path is
   well-tested and handles credential pools correctly.

**Key takeaway:** The code IS wired up for multi-server subagent routing. The gap
is configuration data (empty fallback list) rather than missing code paths.

| Approach | How it works | Pros | Cons |
|----------|-------------|------|------|
| Single provider | Set `delegation.provider` to one server | Simple, explicit | Single point of failure |
| Priority order | Use `subagent_routing.priority_order` with enabled/disabled flags | Rich routing strategy | Not fully read by `delegate_task` |
| Manual fallback | Pass `model={provider: "...", model: "..."}` per-call | Full control | Verbose, must be done each time |
| Delegation fallback (proposed) | `delegation.fallback_providers` chain | Automatic failover | Not yet natively supported |

For production multi-server setups, combine approaches: set a primary
`delegation.provider`, enable all servers in `subagent_routing.priority_order`,
and use manual fallback for specific tasks that need a particular server.

## Subagent Result Location Process

When dispatching parallel async subagents, their results arrive as **inline
assistant messages** in the conversation stream. Finding them requires a
targeted approach — blind text-matching searches often miss results or return
duplicates.

### How Results Arrive

- Each subagent delivers its result as an assistant-role message appended to
  the session's message list.
- The message contains the full response (typically 400–600 words) plus a
  metadata block at the end summarizing what was done, produced, and any
  issues encountered.
- Results are NOT written to disk by default — they live in the session DB.

### Reliable Search Strategy

**Do this:**

1. **Record dispatch IDs.** When dispatching subagents, note their `deleg_` IDs
   (e.g., `deleg_8c7e1fa3`, `deleg_011ea21e`). These appear in the dispatch
   confirmation message and in the `[ASYNC DELEGATION BATCH COMPLETE]` banner.

2. **Anchor on message position, not text matching.** Use `session_search` with
   `role_filter="assistant"` to narrow to assistant messages only. Subagent
   results are always assistant-role — this avoids keyword dilution when the
   parent's own messages also contain relevant terms.

3. **Scroll forward from the last known message.** When a result hasn't appeared
   after its expected duration, scroll *forward* from the most recent message in
   the parent session rather than re-scanning earlier messages. Subagent results
   append at the end of the stream.

4. **Change parameters on each retry.** If the first `session_search` doesn't
   find the result, change something: increase the window size, switch from
   query-mode to scroll-mode, or try a different `around_message_id`. Repeating
   the same query with the same parameters will return the same result (triggering
   the `idempotent_no_progress_warning`).

**Avoid this:**

- Using identical `session_search` parameters repeatedly — if you get the same
  result twice, change the query before trying again.
- Scrolling backward when the result is likely forward — subagent results append
  at the end of the conversation stream.
- Relying solely on FTS5 text matching with broad keywords — "male" and
  "anatomy" may appear in multiple messages, pushing the target result down in
  ranking.

### Verification Pattern for Routing Accuracy

After dispatching subagents, verify correct routing by checking per-child logs:

```bash
# Check which providers each child actually used (not just delegation.provider)
grep -i "acquired provider\|released provider\|subagent.*router" ~/.hermes/logs/agent.log | tail -20

# Verify model names in the log confirm different servers were used
# Example output:
#   Task 1 (dense):  provider=192.168.1.224  model=Qwen3.6-27B-FP8
#   Task 2 (MOE):    provider=192.168.1.221  model=Huihui-Qwen3.6-...
```

**Key insight:** `delegation.provider` is the DEFAULT, not the destination.
When `subagent_routing.enabled: true`, each child calls `acquire_provider()`
independently, walking `priority_order` — so different subagents can go to
different servers even though `delegation.provider` points to one value.

## Provider Capacity and Task Queuing

When dispatching subagents, providers may be at or near their `max_concurrent`
limit. In this case, tasks **queue** rather than fail or get misrouted to lower-priority servers.

- **Queued (not failed):** The dispatch confirmation shows `status: dispatched` with
  individual task statuses of `"queued"`. This means the provider accepted the task but
  it's waiting for capacity to open. No action needed — tasks proceed in order.
- **Queued vs. routed elsewhere:** If a server is at capacity AND its `enabled: false`,
  the router will skip it and try the next server in `priority_order`. Queuing only happens
  when the server is enabled but simply full.
- **How to tell:** Look at the dispatch response — `"status": "queued"` means waiting;
  a different provider name means routed elsewhere. If you see `"queued"` on multiple tasks,
  the providers are saturated and tasks will execute sequentially.

**Practical impact:** Queued tasks add latency (typically 5–30 seconds per queued task) but
never produce incorrect results. For time-sensitive work, check `max_concurrent` values in
config before dispatching large batches — if your batch size exceeds the sum of all servers'
`max_concurrent`, expect some queuing.

## Transient API Failures During Subagent Execution

Subagents may encounter transient API failures during execution. These are
**expected and non-fatal** — they do not indicate a broken routing path.

### Common Pattern: fetchWebContent Dead Pages

The MCP web-search `fetchWebContent` tool fails when it encounters URLs that
return HTTP 200 but contain **no extractable text** — "dead pages" from the
tool's perspective. This is distinct from network errors or timeouts.

- **Symptom:** Consecutive `fetchWebContent` failures with message:
  `Error: No readable content was extracted from this URL`.
- **Root cause:** The target URLs (often paywalled science.org, cell.com,
  or JavaScript-heavy pages) returned HTML but the text extraction layer found
  no meaningful content to report.
- **Impact:** Minimal — the subagent uses cached search result snippets and
  its own domain knowledge to complete the task without fabricating content.
- **Recovery:** The MCP server remains healthy; only specific URLs fail.
  Subagents that proceed without full content still produce accurate results.

**How to distinguish dead pages from other failures:**

| Error Message | Likely Cause | Action |
|---|---|---|
| "No readable content was extracted" | Dead page (paywall, JS-rendered) | Use cached snippets; no retry needed |
| "Connection refused / timeout" | Network or server down | Retry; may need to re-dispatch |
| "Rate limit exceeded" | Burst of fetch calls | Wait and retry; self-heals quickly |

### Other Transient Failure Patterns

| Tool | Symptom | Impact | Recovery |
|------|---------|--------|----------|
| `fetchWebContent` | Consecutive failures during burst | Minor — uses cached snippets | Recovers in seconds |
| `mcp_open_websearch_search` | Intermittent unreachability | Low — returns cached results | Recovers quickly |
| `session_search` | Returns count=0 for valid messages | Medium — causes looping if not noticed | Scroll-mode bypasses FTS5 ranking |

### Handling Strategy

When a subagent reports transient failures:

1. **Check the tool name** — is it `fetchWebContent` (burst-related) or
   something more fundamental?
2. **Verify content quality** — did the subagent fabricate data, or use cached
   results + domain knowledge?
3. **Don't re-dispatch immediately** — transient failures self-heal. Only
   re-dispatch if the failure persists beyond the expected window (typically
   60–90 seconds for a subagent of this size).
4. **Note the pattern** — recurring burst failures suggest the MCP web-search
   server needs tuning (connection pooling, rate limit adjustments).
