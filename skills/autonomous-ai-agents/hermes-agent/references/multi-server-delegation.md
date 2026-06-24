# Multi-Server Delegation for Hermes Agent

## Session Research Notes (2026-06-20)

Research findings on configuring multiple LAN GPU servers for subagent inference.

---

## Key Concepts from Web Research

### Delegation Configuration (from official docs)

The `delegation` section in `~/.hermes/config.yaml` controls subagent behavior:

```yaml
delegation:
  model: "Qwen3.6-27B-FP8"        # Override model (empty = inherit parent)
  provider: "192.168.1.224"       # Override provider (empty = inherit parent)
  base_url: ""                    # Direct OpenAI-compatible endpoint (takes precedence over provider)
  api_key: ""                     # Falls back to OPENAI_API_KEY
  api_mode: ""                    # Wire protocol: chat_completions, codex_responses, anthropic_messages
  max_iterations: 50              # Max turns per child
  child_timeout_seconds: 0        # Default: 0 = no timeout
  max_concurrent_children: 3      # Parallel children per batch (floor 1, no ceiling)
  max_spawn_depth: 1              # Tree depth cap (1-3). 1 = flat.
  orchestrator_enabled: true      # When false, forces all children to leaf role
```

**Precedence:** `delegation.base_url` > `delegation.provider` > parent provider.
Setting just `model` without `provider` changes only the model name while keeping
the parent's credentials.

### Subagent Provider Inheritance

By default, subagents inherit the parent agent's provider and model. Set
`delegation.provider` and `delegation.model` to route subagents to a different
provider:model pair — e.g., use a cheap/fast model for narrowly-scoped subtasks
while your primary agent runs an expensive reasoning model.

### Direct Endpoint Override

If you want the obvious custom-endpoint path, set `delegation.base_url`,
`delegation.api_key`, and `delegation.model`. That sends subagents directly to
that OpenAI-compatible endpoint and takes precedence over `delegation.provider`.

---

## Feature #7481: Per-Delegation Fallback Provider Chain

**Problem — clarified (2026-06-20):** `delegate_task` **does** inherit the parent's
`_fallback_chain` at runtime, but the top-level `fallback_providers: []` in config.yaml
is empty by default. So while the inheritance mechanism works, the chain is thin
(just the parent's own provider) unless populated.

**Proposed syntax:**
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

**Alternative:** Have subagents inherit the parent's `fallback_providers` by default
(with an opt-out flag). This is the approach proposed in #7481.

---

## Provider Routing (OpenRouter-specific)

When using OpenRouter, Hermes supports provider routing — fine-grained control over
which underlying AI providers handle requests:

```yaml
provider_routing:
  sort: "price"              # price | throughput | latency
  only: []                   # Whitelist
  ignore: []                 # Blacklist
  order: []                  # Explicit priority order
  require_parameters: false  # Only use providers supporting all parameters
  data_collection: null      # "allow" or "deny"
```

**Note:** Provider routing only applies when using OpenRouter. It has no effect with
direct provider connections (e.g., connecting directly to the Anthropic API).

---

## Subagent Delegation Details

### Single Task vs Batch

Single task: `delegate_task(goal, context, toolsets)`
Batch: `delegate_task(tasks=[{goal, ...}, ...])` runs children in parallel.

### Depth Limit and Nested Orchestration

- `role="leaf"` (default): child cannot delegate further
- `role="orchestrator"`: child retains the delegation toolset
- `max_spawn_depth: 1` = flat (default), so orchestrator is a no-op
- Raise to `2` for orchestrator children to spawn leaf grandchildren
- Cost warning: at `max_spawn_depth: 3` with `max_concurrent_children: 3`, the tree
  can reach 3x3x3 = 27 concurrent leaf agents

### Lifetime and Durability

`delegate_task` is **synchronous** — not durable. It runs inside the parent's current
turn and blocks until every child finishes. If the parent is interrupted (new message,
/stop, /new), all active children are cancelled and their in-progress work is discarded.

For durable long-running work that must survive interrupts:
- `cronjob` (action=create) — schedules a separate agent run
- `terminal(background=True, notify_on_complete=True)` — long-running shell commands

---

## Subagent Provider Hangs (Critical)

**Problem:** When a vLLM/llama.cpp server hangs mid-stream (stops producing
tokens but keeps the HTTP connection alive), the parent agent blocks in
`delegate_task` indefinitely. The user cannot interrupt it with "continue" —
the parent is waiting for a tool call result, not accepting new input.

**Why this happens:** The stale-stream detector in `chat_completion_helpers.py`
is **explicitly disabled for local endpoints** (line 2548-2550). The rationale
is that local providers can take 300+ seconds for prefill on large contexts.
However, this means a genuinely hung server is never detected.

### Three Layers of Defense

#### Layer 1: Pre-flight Health Check (Before Dispatching)

Before sending work to a provider, verify it responds:

```bash
# Quick connectivity check
curl -s --max-time 5 http://192.168.1.224:5678/v1/models | head -c 100

# If this hangs or returns nothing, the server is unresponsive.
# Try the next server in priority_order before dispatching.
```

**Pattern for the agent:** When you have multiple servers available and one
seems slow, check its health endpoint before routing a subagent there.
If `curl --max-time 5` fails, skip to the next server:

```python
# If 224 is unresponsive, explicitly route to 222:
delegate_task(
    goal="...",
    context="...",
    model={"provider": "192.168.1.222", "model": "Huihui-Qwen3.6-..."}
)
```

#### Layer 2: `child_timeout_seconds` (Hard Cap)

Set a hard timeout so hung subagents eventually get killed instead of
running forever:

```yaml
# config.yaml
delegation:
  child_timeout_seconds: 600  # 10-minute hard cap per subagent
```

**Trade-off:** Legitimate heavy work (deep code review, large research) can
take longer than 10 minutes. Set this based on your typical subagent task
duration. The minimum enforced value is 30 seconds.

When a timeout fires:
- The child is killed and returns a `timeout` status to the parent.
- If the child made 0 API calls, a diagnostic is written to
  `~/.hermes/logs/subagent-timeout-<id>-<timestamp>.log`.
- If the child made API calls, the error message indicates how many completed.

#### Layer 3: Force Stale-Stream Timeout for Local Providers

Override the default behavior that disables stale detection for local
endpoints. Set this environment variable in your `.env` or shell profile:

```bash
# Kill streams that produce no tokens for 300 seconds, even on local providers
export HERMES_STREAM_STALE_TIMEOUT=300
```

**Or configure per-provider** in `config.yaml`:

```yaml
providers:
  "192.168.1.224":
    stale_timeout_seconds: 300
  "192.168.1.222":
    stale_timeout_seconds: 300
```

This makes the stale-stream detector active even for LAN servers. A stream
that produces no tokens for 300s gets killed, the connection resets, and the
retry loop can fall back to the next provider.

**Recommended values:**
- **300s (5 min):** Safe for most workloads. Catches hung servers without
  killing legitimate long-thinking models.
- **600s (10 min):** If your models legitimately think for extended periods.
- **180s (3 min):** Aggressive — catches hangs faster but risks killing
  healthy streams during prefill on large contexts.

### Combined Recommendation

For production multi-server setups, use all three layers:

```yaml
# config.yaml
delegation:
  child_timeout_seconds: 600  # Hard cap

providers:
  "192.168.1.224":
    stale_timeout_seconds: 300
  "192.168.1.222":
    stale_timeout_seconds: 300
```

Plus `export HERMES_STREAM_STALE_TIMEOUT=300` as a global safety net.

### What to Do When a Subagent Hangs

1. **You cannot "continue" a hung subagent.** The parent is blocked in
   `_run_single_child()` — it's not accepting new user messages. The hung
   server's context is likely already invalidated.

2. **Wait for the timeout** (if configured). The child will be killed and
   return an error. Then re-dispatch to a different server.

3. **If no timeout is configured**, the only option is to kill the parent
   agent process (`/stop` or Ctrl+C) and restart. This is why configuring
   Layer 2 (`child_timeout_seconds`) is critical.

4. **After recovery**, check the server that hung:
   ```bash
   # Is the server still responding?
   curl -s --max-time 5 http://192.168.1.224:5678/v1/models

   # Check server logs for OOM, GPU errors, etc.
   ```

### Why "Continue" Doesn't Work

When a subagent is running, the parent agent is blocked in
`_run_single_child()` waiting for the child's conversation loop to complete.
The parent is not in its main loop — it cannot accept new user messages.
Sending "continue" to the provider via the parent is impossible because the
parent has no control over the child's active API connection. The child owns
that connection, and if the server stops responding, the child's httpx client
is stuck in a read operation.

The only recovery paths are:
- **Timeout kills the child** (requires `child_timeout_seconds` or stale-stream timeout)
- **Kill the parent process** (loses all work in progress)
- **The server recovers** (unpredictable; context may already be lost)

---

## Sources

- https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation/
- https://hermes-agent.nousresearch.com/docs/user-guide/configuration/
- https://hermes-agent.nousresearch.com/docs/user-guide/features/provider-routing/
- https://github.com/NousResearch/hermes-agent/issues/7481
