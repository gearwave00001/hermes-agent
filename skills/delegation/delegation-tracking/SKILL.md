---
name: delegation-tracking
description: "Track server IPs and runtimes for subagent delegation runs."
version: 1.0.0
author: Hermes Agent
tags: [delegation, tracking, servers, runtime]
---

# Delegation Tracking

When dispatching subagents via `delegate_task()`, always record and report:

## At Dispatch Time
- **Record each delegation_id** in the tool call output
- **Note the server IP** for each subagent (check which `model` field is set — it routes to a specific GPU)
- **Check `subagent_routing` config before assigning server IDs.** If `exclude_from_subagents: true`, the main_server (225) is excluded from the priority_order pool. Only dispatch to servers listed in `priority_order`: 224, 222, 223, 221 — NOT 225.
- Report them to the user explicitly, e.g.:
  ```
  Subagent 1 → 192.168.1.224 (deleg_abc123)
  Subagent 2 → 192.168.1.222 (deleg_def456)
  ```

## At Completion Time
- Parse the completion banner for:
  - `delegation_id` (match against recorded IDs)
  - `model` (confirms which server was used)
  - `duration_seconds` or `Total duration`
- Report per-subagent timing in the final summary.

## Why This Matters
When running multiple subagents, latency can increase due to:
1. Queue buildup on llama.cpp servers (sequential processing)
2. KV cache fragmentation with large context windows
3. Connection setup overhead if keep-alive is disabled

Tracking server assignments helps diagnose whether slow responses are from specific servers or systemic.

## Common Pitfall: Wrong Server ID in File Names
When dispatching subagents and naming output files with server IDs, verify the `subagent_routing` config:
- `main_server` (225) may be excluded via `exclude_from_subagents: true`
- Only servers in `priority_order` are valid subagent destinations
- If you label a 4th subagent as "225" but it actually ran on the fallback (221), rename the output file accordingly

## Common Pitfall: Subagent CWD Mismatch on Remote Servers
When dispatching multiple subagents across different servers, each subagent writes output relative to ITS OWN working directory, not necessarily the same path as the parent session. This means:
- A subagent targeting server 224 may write to `/workspace/instruct-test/test-3/` on its own machine, which could be a different mount point or path than the parent's `test-3/`.
- Always verify output files landed in the expected location — check with `find / -name "<filename>" -newermt "<dispatch_time>"` if unsure.
- When all subagents target local containers sharing a volume, this is not an issue. But for servers on different machines (e.g., 221–224 via SSH), each has its own workspace root.
- If a file appears "missing" from the expected path, search broadly: `find / -name "*<server-id>*results.svg"` to locate it.

### Subagent Workspace Path Bug Pattern
When dispatching subagents with explicit server assignments (e.g., via `context` field in `delegate_task`), each child resolves its workspace relative to its own server's mounted filesystem. The parent's `workspace/` path and the child's `workspace/` path may point to different inodes if:
- Servers are on separate machines with independent mounts
- The subagent's CWD is resolved at dispatch time before SSH/connection setup

**Symptom**: A file "missing" from the expected directory, but the server reports it completed successfully. The file exists — just not where you looked.

**Fix**: After dispatch, verify output files by searching across all known workspace roots:
```bash
# Search broadly for any file matching the pattern and created after dispatch
find / -name "<filename>" -newermt "<dispatch_time>" 2>/dev/null
```
Or check each server's workspace individually.

## Subagent Monitoring and Logging
Subagents produce their own logs that you can monitor and review:

### Where subagent logs live
- **Main log** (`~/.hermes/logs/agent.log`): All subagent lines are prefixed with `[subagent-N]` via `log_prefix`. Filter with:
  ```bash
  hermes logs | grep "subagent"
  hermes logs --level INFO
  hermes logs -f | grep "subagent\|Stream drop\|API call"
  ```
- **Per-session JSON logs** (`~/.hermes/logs/session_{session_id}.json`): Full conversation trajectory for each subagent. Find session IDs in the completion banner or agent.log.
- **Per-action log files**: Each subagent gets a dedicated log file under `~/.hermes/logs/` named by its action ID, separate from the main agent.log.

### Key log fields (from stream_diag.py)
When debugging slow or failed subagents, look for these fields in log lines:
- `subagent_id` — which child agent (e.g., "221", "224")
- `depth` — nesting level (1 = direct child, 2 = grandchild)
- `provider` — which server it's connected to (e.g., "192.168.1.224")
- `base_url` — the endpoint URL
- `error_type`, `chain` — exception details for debugging drops
- `bytes`, `chunks`, `elapsed` — stream diagnostics

### Debugging tips
- **Stream drops**: Filter with `hermes logs --level WARNING | grep "stream drop"` to find mid-flight disconnections.
- **Context overflow**: If a subagent takes unusually long and the parent session overflows, check for `[subagent-N]` lines with high `elapsed` times — the overflow may be on the parent side, not the subagent.
- **File location**: When a file appears "missing," first confirm the subagent completed (check agent.log for completion banner), then search broadly across workspace roots.

## Custom Providers Reference
| Server | IP | GPU | Model | Subagent Target? |
|--------|----|-----|-------|------------------|
| 225 | 192.168.1.225 | RTX 5060 Ti 16GB | Huihui-Qwen3.6-35B-A3B-abliterated-ggml-model-Q6_K.gguf | NO (main_server, excluded) |
| 224 | 192.168.1.224 | 2x R9700 PRO AI 32GB (64GB VRAM) | Qwen3.6-27B-FP8 | YES (priority: 1st) |
| 222 | 192.168.1.222 | RTX 5090 32GB | Huihui-Qwen3.6-27B-abliterated-ggml-model-Q6_K.gguf | YES (priority: 2nd) |
| 223 | 192.168.1.223 | RTX 5080 | Huihui-Qwen3.6-35B-A3B-abliterated-ggml-model-Q6_K.gguf | YES (priority: 3rd) |
| 221 | 192.168.1.221 | RX 7900 XTX | Huihui-Qwen3.6-35B-A3B-abliterated-ggml-model-Q6_K.gguf | YES (priority: 4th) |
