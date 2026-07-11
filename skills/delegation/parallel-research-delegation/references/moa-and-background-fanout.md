# MoA and Background Fan-Out — Architecture Reference

## Background Fan-Out (upstream PR #49734, merged Jun 20, v2026.7.1)

When the model calls `delegate_task`, the chat is NO LONGER blocked. Key behaviors:

- **Single-task and batch both run in background.** Parent turn returns immediately with a handle.
- **One consolidated return.** When all subagents finish, results re-enter as a single combined block.
- **One async-pool slot.** The entire batch occupies a single slot, not N.
- **Orchestrators stay sync.** Subagents at `_delegate_depth > 0` still run synchronously — they need results within their own turn.
- **Fallback on capacity.** If async pool is full, batch runs inline synchronously.

**Key files:** `tools/async_delegation.py` (`dispatch_async_delegation_batch`, `_finalize_batch`), `tools/delegate_tool.py` (`_execute_and_aggregate()`), `tools/process_registry.py` (consolidated rendering).

## Local Subagent Router (tools/subagent_router.py)

The local `subagent_routing:` config layer sits **before** the background fan-out decision. It intercepts per-child credential resolution at `delegate_tool.py` ~line 2318, then the normal async dispatch proceeds. The two layers are orthogonal:

| Layer | Concern | Config |
|---|---|---|
| Background fan-out | *When* subagents run (non-blocking) | Automatic, no config |
| Subagent router | *Where* subagents run (which GPU) | `subagent_routing:` in config.yaml |

When routing is disabled/absent, all children share the same credentials from `_resolve_delegation_credentials()` — the original upstream behavior.

## Mixture-of-Agents (MoA)

MoA is a **different mechanism** from subagent delegation. Do not confuse them:

| | MoA | Subagent delegation |
|---|---|---|
| What fans out | Reference model LLM calls (no tools, pure advisory text) | Full AIAgent instances (with tools, terminal, file access) |
| Concurrency | `ThreadPoolExecutor`, 8 workers max | Per-provider `max_concurrent` + overflow queue |
| When | During a `/moa` turn, before each model iteration | When model calls `delegate_task` |
| Who decides | Preset config (static ensemble) | Priority routing + goal rules + per-task override |
| Result | Reference opinions injected into aggregator context | Independent subagent summaries returned to parent |

### MoA is NOT locked to frontier providers

The default preset uses GPT-5.5 + Claude Opus 4.8, but any provider works. A MoA preset slot is just `{provider, model}` — resolved through `resolve_runtime_provider()`, the same resolver the subagent router uses. The only rejection is `provider: "moa"` (prevents recursive MoA trees).

**Example — local GPU MoA preset:**
```yaml
moa:
  presets:
    local-council:
      reference_models:
        - provider: "gpu-server-1"
          model: "Qwen3.6-27B-FP8"
        - provider: "gpu-server-2"
          model: "other-model"
      aggregator:
        provider: "gpu-server-1"
        model: "Qwen3.6-27B-FP8"
```

MoA runs at the `call_llm` level, not through `delegate_task`, so the subagent router does NOT apply to MoA reference/aggregator slots directly. A MoA aggregator can itself call `delegate_task`, and those delegated subagents would be routed through the local router.

### Key files
- `agent/moa_loop.py` — runtime (`_slot_runtime()`, `_run_reference()`, `_run_references_parallel()`)
- `hermes_cli/moa_config.py` — preset normalization (`_clean_slot()`, `_normalize_preset()`)
- `agent/model_metadata.py` — MoA context-length resolution (line 1717, resolves from aggregator slot)
