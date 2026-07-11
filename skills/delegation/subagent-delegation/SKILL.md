---
name: subagent-delegation
description: "Dispatch, track, and verify subagents via delegate_task()."
version: 2.0.0
author: Hermes Agent
tags: [delegation, subagents, tracking, async, patterns, moe-routing, backchannel]
---

# Subagent Delegation

Best practices for dispatching, tracking, and verifying background subagents via `delegate_task()`.

## What Leaf Subagents CAN and CANNOT Do

Leaf subagents (the default) have a restricted but capable toolset:

**Available:** `web_search`, `read_file`/`write_file`/`patch`, `terminal`, `browser_*`, `vision_analyze`, `browser_console`, `browser_press`

**Blocked:**

| Tool | Why | Impact |
|------|-----|--------|
| `clarify` | No interactive UI in a child | Cannot ask parent for guidance mid-run |
| `delegate_task` | Depth=1, leaf role | Cannot spawn grandchildren |
| `memory` | No persistent bank access | Cannot save durable facts |
| `send_message` | No cross-session messaging | Result is fire-and-forget via completion queue |
| `execute_code` | Not in leaf toolset | Must use `terminal` for multi-step code |

Orchestrator subagents retain `delegate_task` but still cannot use `clarify`, `memory`, `send_message`, or `execute_code`.

## Delegation Config

```yaml
delegation:
  max_concurrent_children: 10   # WIDTH — max parallel children per batch
  max_spawn_depth: 1             # DEPTH — 1 = flat (default), 2 = child→grandchild
```

- **`max_concurrent_children`**: Batch-size cap. Governs both sync and background delegations. Above 10 triggers a cost warning (each child independently consumes tokens).
- **`max_spawn_depth`**: Default 1 means flat — children cannot delegate further. Raise to 2 for orchestrator children.
- **Multiplication rule**: At depth 3 with width 3, effective concurrency = 3×3×3 = 27 leaf agents.

## The Backchannel Gap

Subagents are **fire-and-forget**. They receive `goal` + `context` at dispatch, work independently, and deliver a single result. There is **no mid-run channel** for them to ask the parent questions or signal they need guidance.

**Workarounds:**
- Subagent makes its own calls (web_search, read_file) and proceeds on judgment
- Parent mediates: subagent finishes, parent decides if more work is needed
- Use shared storage (filesystem files) as a backchannel

## MOE Routing vs Delegation

These are orthogonal systems that work together:

- **Delegation** = parallel child agents with independent context and tools
- **MOE / provider routing** = which GPU server handles each API call

When you dispatch subagents, the router calls `acquire_provider()` which resolves credentials, health-checks, and assigns a server slot.

```
PARENT (server 224, excluded from subagents)
   ├── delegate_task → SUBAGENT #1 → MOE routes to server 222
   └── delegate_task → SUBAGENT #2 → MOE routes to server 223
```

## Server Pool (Single Source of Truth)

| Server | IP | GPU | Model | Subagent Target? |
|--------|----|-----|-------|------------------|
| 224 | 192.168.1.224 | 2x R9700 PRO AI 32GB (64GB VRAM) | Qwen3.6-27B-FP8 | NO (main_server, excluded) |
| 222 | 192.168.1.222 | RTX 5090 32GB | Huihui-Qwen3.6-27B-abliterated-ggml-model-Q6_K.gguf | YES (priority: 2nd) |
| 223 | 192.168.1.223 | RTX 5080 | Huihui-Qwen3.6-35B-A3B-abliterated-ggml-model-Q6_K.gguf | YES (priority: 3rd) |
| 221 | 192.168.1.221 | RX 7900 XTX | Huihui-Qwen3.6-35B-A3B-abliterated-ggml-model-Q6_K.gguf | YES (priority: 4th) |
| 225 | 192.168.1.225 | RTX 5060 Ti 16GB | Huihui-Qwen3.6-35B-A3B-abliterated-ggml-model-Q6_K.gguf | YES (priority: 5th) |

**NOTE:** Server 224 is currently the main_server (as of Jul 11, 2026 model switch). The main_server can change — always check `grep "main_server:" ~/.hermes/config.yaml` before assuming which server is excluded. Also verify `delegation.provider` does NOT point to the excluded server (see `delegation-routing` skill for the sync pitfall).

## Config Validation Pitfalls

### Typos in priority_order silently skip servers

The router iterates `priority_order` entries and skips any with `enabled: false` or an unresolvable `name`. Common typos that cause silent failures:

- **Colon instead of dot in IP**: `192:168.1.225` instead of `192.168.1.225` — the router cannot resolve this as a valid provider URL, so the server is silently skipped.
- **Misspelled `enabled` key**: `enbabled: true` instead of `enabled: true` — the router reads `enabled` as `None`/`False`, so the server is treated as disabled.

**Symptom**: A server listed in `priority_order` never receives subagent requests despite appearing healthy.
**Fix**: Validate the config — `grep -A 5 "name:" ~/.hermes/config.yaml` under `priority_order` and check for typos. A quick health check (`curl http://<ip>:5678/v1/models`) confirms the server itself is reachable.

### `exclude_from_subagents` vs actual dispatch behavior

When `main_server` is set with `exclude_from_subagents: true`, the main server should NOT receive subagent requests. Verify actual dispatch by checking logs:

```bash
grep "OpenAI client created.*subagent-router-dispatch" ~/.hermes/logs/agent.log | grep "<main_server_ip>"
```

If the main server IS receiving requests despite the exclusion flag, the exclusion logic may have a bug — check `tools/subagent_router.py` for the `_is_excluded()` path.

## Dispatch Phase (MANDATORY)

Immediately after calling `delegate_task()`:

1. **Record delegation IDs** from tool output (`deleg_XXXXXX`)
2. **Report server assignments** by running:
   ```bash
   bash ~/.hermes/scripts/subagent-watch.sh
   ```
   This reads `agent.log` and outputs a formatted table with delegation_id, session, model, provider IP, and time.
3. **Check config before assigning server IDs.** If `exclude_from_subagents: true`, the main_server is excluded. Only dispatch to `priority_order` servers that have valid `name` (no typos) and `enabled: true`. **Beware**: config typos like `192:168.1.225` (colon instead of dot) or `enbabled: true` (misspelled key) silently skip servers — see "Config Validation Pitfalls" below.

**Flexible routing for queued subagents:** When dispatching 5+, the first subagents take their preferred server immediately, but **queued subagents use flexible priority-based routing** — they are NOT locked to predetermined servers. As capacity opens, queued subagents pick whichever has room. Do not assume a queued subagent will run on a specific server.

### Dispatch Summary Format

```
DELEGATION ID          GOAL                              SERVER              MODEL
---------------        ----                              ------              ----
deleg_2ecfe30c         Weather patterns                  192.168.1.224       Qwen3.6-27B-FP8
deleg_ad82cd04         Global warming impact             192.168.1.222       Huihui-Qwen3.6-27B-abliterated-ggml-model-Q6_K.gguf
```

## Completion Phase

**DO NOT give a premature final summary.** Wait for ALL `[ASYNC DELEGATION BATCH COMPLETE]` banners before reporting results.

**Correct sequence:**
1. Dispatch → record delegation IDs, servers, models, goals
2. Report the *dispatch table* (not a final summary)
3. **Wait for ALL completion banners** — when the user says "just hold on," do not interject with partial results
4. Verify output files landed: `ls -lh <target_dir>/`
5. Only then give the final summary with verified results

### Staggered Write Timing

Files do not all land simultaneously. There is a **~60–90 second staggered write window** after dispatch, especially for queued subagents:
- First 3 of 5 files typically write within 15–30 seconds of each other
- Queued subagents write later (4th and 5th may not land until 60–90 seconds after dispatch)
- **Best practice**: Use `find / -name "<pattern>" -newermt "<dispatch_time>"` to catch all files regardless of when they land
- If checking `ls` immediately after the last banner and some files are missing, wait 30–60 seconds and re-check

### Multi-subagent Concurrency on Shared Servers

When two subagents land on the same server, they share the auxiliary compression resource. One may compact context mid-flight (40→19 messages, ~130k→51k tokens) — this is **normal behavior**, not a sign of trouble.

## File Output Verification

Subagents write relative to their own working directory. After completion:

```bash
# Verify files exist and have content
ls -lh <target_dir>/

# If a file appears missing, search broadly (subagent CWD may differ)
find / -name "<filename>" -newermt "<dispatch_time>" 2>/dev/null
```

### CWD Mismatch on Remote Servers (Common Pitfall)

Each subagent resolves its workspace relative to ITS OWN server's mounted filesystem. The parent's `workspace/` path and the child's `workspace/` path may point to different inodes if servers are on separate machines with independent mounts.

**Symptom:** A file "missing" from the expected directory, but the server reports it completed successfully.
**Fix:** Search broadly — `find / -name "<filename>" -newermt "<dispatch_time>" 2>/dev/null`

### Wrong Server ID in File Names (Common Pitfall)

Verify `subagent_routing` config — `main_server` (225) may be excluded. Only servers in `priority_order` are valid subagent destinations. If you label a 4th subagent as "225" but it actually ran on the fallback (221), rename the output file accordingly.

### write_file Log Entry Is Misleading

The log shows `tool write_file completed (0.24s, 376 chars)` — the ~370 chars is the **tool's confirmation message**, NOT the file content. The actual content was in the model's output tokens on the preceding API call. Verify actual file size with `wc -l <file>` or `ls -lh`.

## Subagent Monitoring and Logging

### Where subagent logs live
- **Main log** (`~/.hermes/logs/agent.log`): All subagent lines prefixed with `[subagent-N]`. Filter with `hermes logs | grep "subagent"`
- **Per-session JSON logs** (`~/.hermes/logs/session_{session_id}.json`): Full conversation trajectory
- **Per-action log files**: Each subagent gets a dedicated log file under `~/.hermes/logs/` named by its action ID

### Key log fields
- `subagent_id` — which child agent (e.g., "221", "224")
- `depth` — nesting level (1 = direct child)
- `provider` — which server (e.g., "192.168.1.224")
- `base_url` — the endpoint URL
- `error_type`, `chain` — exception details
- `bytes`, `chunks`, `elapsed` — stream diagnostics

### Subagent Session Storage

Subagent sessions are stored in `~/.hermes/logs/agent.log` with full conversation context. Each subagent gets a unique session ID marked with `platform=subagent`:

```bash
# Filter to see all subagent session entries
grep "platform=subagent" ~/.hermes/logs/agent.log | grep -E "(delegation_id)"

# See the full conversation for a specific subagent
grep "20260707_025248_5f9209" ~/.hermes/logs/agent.log
```

Each entry includes: session ID, `platform=subagent` marker, `history=0` (starts fresh), and full message content.

### Debugging tips
- **Stream drops**: `hermes logs --level WARNING | grep "stream drop"`
- **Context overflow**: Check for `[subagent-N]` lines with high `elapsed` times
- **File location**: Confirm subagent completed first, then search broadly across workspace roots

## Tool Fallback Pattern

If `web_search` fails (missing Firecrawl API key), fall back to:
- `mcp_open_websearch_search` — DuckDuckGo search
- `mcp_open_websearch_fetchWebContent` — fetch page content

**Camofox IS available to subagents.** Subagents run in threads (same process), inheriting `os.environ` including `CAMOFOX_URL` and the parent's `enabled_toolsets`.

**Automatic fallback:** When `fetchWebContent` fails on JS-rendered sites, it automatically falls back to `browser_navigate` + `browser_snapshot` via Camofox. Wired in `tools/mcp_tool.py`.

## Stuck Queue Detection

When dispatching 8-10+, some subagents queue due to capacity limits. They normally start within 60-90 seconds, but can get stuck for 3+ minutes even with all servers idle.

**Root causes (fixed Jul 2026):** Six bugs in `tools/subagent_router.py` — health check inside global lock, missing `_dispatching` flag checks, wrong provider released on failure, etc. See `references/queue-stall-detection.md` for full analysis.

**Detection workflow:**
1. Check servers: `curl http://192.168.1.22{1-4}:5678/v1/models` — if all respond, servers are healthy
2. If healthy but subagents haven't started after 3+ minutes:
   ```python
   from tools.subagent_router import _state
   st = _state()
   print(f'Dispatcher: {st._dispatcher_running}, Queue: {len(st._pending_queue)}, Dispatching: {st._dispatching}')
   ```
3. If dispatcher is dead and files are missing → re-dispatch the gap

**Action:** Re-dispatch each missing subagent with a fresh `delegate_task()`. Use the same goal/context. No harm in re-dispatching — old tasks eventually complete too.



## All-Queued Saturation (4+ Simultaneous Dispatches)

When 4+ subagents dispatch simultaneously and all providers are at capacity, ALL tasks get queued. Their results eventually re-enter via the dispatcher — this is **expected behavior**, not a bug.

Detection: `"All N tasks were queued due to provider capacity limits."` in dispatch output. Unlike stuck queues, fleet-dispatch queuing is **temporary** — the dispatcher picks them up within 2–4 minutes.

## Related Skills

- `delegation-routing` — Router internals, provider acquisition, dead dispatcher bugs, TOCTOU race, completion drain path
- `parallel-research-delegation` — Multi-subagent research workflow with compiled reports

## Reference Files

- `references/queue-stall-detection.md` — Root cause analysis for stuck queue bugs
