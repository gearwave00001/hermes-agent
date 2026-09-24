# CUSTOMIZATIONS.md — Local Additions to Base Hermes (Regression Pin)

This document is the **regression-testing pin** for everything this repository adds on
top of upstream `hermes-agent`. It was written **before** the upstream merge
(merge-base `67e73ae9`, 2026-07-20; local head `2bdd0decd2`, 2026-07-28).

Purpose: after merging/rebuilding on top of upstream's overhaul (~24.6k commits),
every item below must either (a) survive byte-for-byte, (b) be re-homed to the new
module layout with behavior preserved, or (c) be explicitly dropped with sign-off.
The pre-merge blob SHAs let you verify provenance of every file without ambiguity.

## Provenance anchors

| Anchor | SHA | Date |
|---|---|---|
| Upstream merge-base (our fork point) | `67e73ae9` | 2026-07-20 |
| Local head before merge | `2bdd0decd2` | 2026-07-28 |
| Backup branch (pre-merge state) | `backup/pre-upstream-merge-2026-09` | — |

Our side of the divergence: **21 commits, 36 files**.

## Commit history (ours, oldest → newest)

```
ba821490a3  2026-06-24  Initial support for configuring multiple local workstations as subagent providers
e55ecb584a  2026-06-25  hermes-agent SKILL.md fix batched subagent queueing
28a385e913  2026-06-29  implement tool calling delay to resolve duckduckgo timeouts with 4 parallel subagents
ffafd0b55c  2026-06-29  hermes-agent subagent delegation search refinement with 4 endpoints
8704ae4596  2026-06-29  mnemosyne bookmark skill
414d6e1e79  2026-07-01  push my sandbox script so it will stop disappearing
e3fc2d5565  2026-07-01  moved my startup script to untracked folder not visible to sandbox docker
fe6b8be4b8  2026-07-02  commiting sbx startup script again after several accidental removals - don't merge to main
ea890c1207  2026-07-06  update startup script
201bc84c75  2026-07-06  fix: ignore FUSE mount hidden files in docker/
0d7a3a793e  2026-07-06  Revert "fix: ignore FUSE mount hidden files in docker/"
7dc39f2be9  2026-07-09  tools/subagent_router.py — TOCTOU race fix in provider acquisition
703c6af387  2026-07-11  fix dead dispatcher with larger amount of queued subagents
429b362eb3  2026-07-11  fix model switching - main server update in subagent block
a036a43153  2026-07-11  skill updates, reference consolidations, clean up before merging in upstream changes
51a5046976  2026-07-21  fortify camofox fallback rules
62319911b2  2026-07-25  claude subagent tracker polling
8ffcaced7b  2026-07-26  Support for tracking headless claude subagents
bc72e50870  2026-07-26  claude subagent watch fix
c9ed8f8512  2026-07-26  fix: prevent duplicate/stale event delivery and fill config gaps
2bdd0decd2  2026-07-28  delegation updates
```

## File inventory (status vs merge-base, pre-merge blob SHA, lines)

### New files (A) — must survive intact or be consciously retired

| File | Lines | Pre-merge blob | Feature |
|---|---|---|---|
| `tools/subagent_router.py` | 1034 | `7069d25e089c` | CORE: priority-pool subagent routing across local GPU servers |
| `tests/tools/test_subagent_router.py` | 1047 | `5e2e39269c72` | Router unit tests |
| `tests/tools/test_subagent_router_deadlock.py` | 645 | `0b9ce7b915b8` | Dispatcher deadlock regression tests |
| `tests/tools/test_mcp_tool_delay.py` | 201 | `946513fff835` | MCP pre-call delay tests |
| `tests/memory/test_mnemosyne.py` | 28 | `4b1edeb2c061` | Mnemosyne smoke tests |
| `docker/sandbox-start.sh` | 24 | `5e1e4e7b89c5` | SBX sandbox startup script (local infra) |
| `skills/bookmarks/SKILL.md` | 67 | `976f8e445f15` | Bookmark skill (Mnemosyne-backed URL tracking) |
| `skills/bookmarks/scripts/bookmarks.py` | 298 | `44e7572f9a63` | Bookmark CLI script |
| `skills/bookmarks/references/setup.md` | 64 | `265e6f3d4aee` | Setup reference |
| `skills/bookmarks/bookmarks/SKILL.md` | 47 | `7c9a61b58b6d` | Nested copy (see note below) |
| `skills/bookmarks/bookmarks/scripts/bookmarks.py` | 298 | `44e7572f9a63` | Nested copy, identical blob to top-level |
| `skills/delegation/delegation-routing/SKILL.md` | 364 | `22402f92d59d` | Delegation routing playbook |
| `skills/delegation/delegation-routing/references/completion-drain-path.md` | 64 | `2aaa5ca211e9` | Completion drain path reference |
| `skills/delegation/delegation-routing/references/dispatcher-death-6-parallel-fix.md` | 49 | `f21e9c6cefe0` | Dispatcher death fix reference |
| `skills/delegation/delegation-routing/references/toctou-race-fix.md` | 81 | `b320795d6c17` | TOCTOU race fix reference |
| `skills/delegation/parallel-research-delegation/SKILL.md` | 105 | `d974f2e6973d` | Parallel research fan-out playbook |
| `skills/delegation/parallel-research-delegation/references/beer-research-example.md` | 23 | `9d7acab8e97e` | Example run |
| `skills/delegation/parallel-research-delegation/references/moa-and-background-fanout.md` | 62 | `a204ce72f241` | MoA/fan-out reference |
| `skills/delegation/subagent-delegation/SKILL.md` | 251 | `41798713e4d6` | Subagent delegation playbook |
| `skills/delegation/subagent-delegation/references/queue-stall-detection.md` | 111 | `09cab9cb74b8` | Queue stall detection reference |
| `skills/autonomous-ai-agents/mcp-clis/SKILL.md` | 126 | `e055113571ae` | MCP-as-CLI discovery skill |
| `skills/autonomous-ai-agents/mcp-clis/references/curl-discovery.md` | 68 | `f691db52fff3` | curl-based endpoint discovery |

Note: `skills/bookmarks/bookmarks/*` duplicates `skills/bookmarks/*` (identical blob
`44e7572f9a63`). Likely an accidental nested copy — candidate for cleanup during the
rebuild (keep one canonical location).

### Modified files (M) — ours overlaid on merge-base versions

| File | Base→Head lines | Pre-merge blob | Our delta |
|---|---|---|---|
| `tools/delegate_tool.py` | 3656→3811 | `4be71e453989` | Provider-acquisition seam per child task (acquire/enqueue/release via subagent_router); `provider` field in result entries |
| `tools/async_delegation.py` | 940→1017 | `89f35e8e49c4` | Delivery-attempt bookkeeping (`_MAX_DELIVERY_ATTEMPTS=50`, `give_up_completion_delivery`), per-event platform/chat_id/chat_type routing metadata, bool-return `complete_event_delivery` |
| `tools/process_registry.py` | 2353→2417 | `3fdd83a5c85d` | `skip_async_delegation` kwarg on `drain_notifications`; structured skipped/dropped logging; `_format_age` helper |
| `tools/mcp_tool.py` | 5913→6088 | `f456ff77318e` | Pre-call delay wrapping `_make_tool_handler` (config `agent.tool_call_delay_ms` / `agent.mcp_delay_tool_patterns`); camofox fallback rules for `fetchWebContent` in `_register_server_tools` |
| `cli.py` | 16089→16225 | `d332cd4902ff` | `_owns_process_notification` ancestor-chain extension; `_async_delegation_watcher` thread; `_idle_completion_poller`; model-switch call-site wiring |
| `gateway/run.py` | 23080→23130 | `455a0b4a8ece` | `_get_fallback_session_source` (route unroutable completion events to most recent cached channel); deliver-before-format ordering in delivery loop |
| `hermes_cli/config.py` | 9232→9248 | `d7dddf8961fb` | Config defaults (see Config keys below) |
| `hermes_cli/model_switch.py` | 2855→2891 | `9476f6c01c49` | `_update_main_server_on_switch` — keep main conversation server out of subagent routing when model switches |
| `tui_gateway/server.py` | 15930→15935 | `e8b3b3d706b1` | `_persist_model_switch` main-server sync call site |
| `cli-config.yaml.example` | 1508→1590 | `81530671cc3f` | Documented `subagent_routing` example block |
| `tests/cli/test_cli_async_delegation_delivery.py` | 76→86 | `e7bee38dcfb6` | Duplicate/stale delivery regression cases |
| `tests/tools/test_process_registry.py` | 2231→2511 | `749e91453c6b` | Drain ownership/logging tests |
| `skills/autonomous-ai-agents/hermes-agent/SKILL.md` | 1111→836 | `8bada2f8fbe3` | Batched subagent queueing guidance, delegation endpoint refinements |
| `skills/autonomous-ai-agents/claude-code/SKILL.md` | 745→839 | `2815aed265bd` | Headless claude subagent tracking guidance |

## Config keys added (in `DEFAULT_CONFIG`, pre-merge home: `hermes_cli/config.py`)

1. `agent.tool_call_delay_ms` (default `0`) — ms delay before MCP tool calls matching
   the patterns; prevents DuckDuckGo thundering-herd timeouts under 4+ parallel
   subagents.
2. `agent.mcp_delay_tool_patterns` (default `["*search*", "*fetch*"]`) — glob patterns
   selecting which MCP tool names get the delay.
3. `subagent_routing.claude_code.max_turns` (default `50`) — `--max-turns` for
   `claude -p` headless subagent calls.
4. `subagent_routing.claude_code.allowed_tools` (default `"Read,Write,Bash,Grep,Edit"`)
   — `--allowedTools` for the same.

Plus user-defined (not in DEFAULT_CONFIG): the full `subagent_routing` topology block
(`enabled`, `health_check_timeout`, `queue.{enabled,max_size,poll_interval}`,
`goal_rules[]`, `priority_order[].{name,purpose,enabled,max_concurrent,active_count}`,
`main_server`, `exclude_main_from_subagents`) — documented in
`cli-config.yaml.example`. Consumed by `tools/subagent_router.py`.

Post-merge these move to `hermes_cli/config_defaults.py` (upstream relocated
DEFAULT_CONFIG). The keys themselves are unchanged.

## Behavioral contract (what "working" means after the rebuild)

### C1 — Priority subagent routing (the core feature; nothing upstream replaces it)
- With `subagent_routing.enabled: true`, `delegate_task` tasks are distributed across
  `priority_order` servers by priority, respecting per-provider `max_concurrent`.
- Slot acquisition is reserve-first (TOCTOU-safe): reserve slot → health check →
  proceed; failed health check releases the slot.
- Per-task `provider:` override beats goal_rules, which beat priority_order.
- Overflow: when all providers are at capacity, tasks enter the overflow queue
  (`queue.poll_interval` dispatch thread); synchronous batches block until queued
  tasks complete.
- On subagent completion the provider slot is released exactly once
  (`_assigned_provider` set even for enqueued tasks — phantom-capacity bug class).
- Dead-dispatcher protection: large queued batches cannot kill the dispatcher thread.
- Main conversation server excluded from subagent pools when
  `exclude_main_from_subagents: true` (including after a `/model` switch).

### C2 — Async delegation delivery integrity
- No duplicate completions: an event delivered to a session is not redelivered to the
  same or an ancestor session (positive-proof ownership, compression-lineage aware).
- No stale deliveries: events restored from a previous process are not drained into a
  session that cannot prove ownership.
- Give-up path: after max delivery attempts the event reaches a terminal state instead
  of looping forever (upstream equivalent: `_MAX_DELIVERY_ATTEMPTS=8` → `dropped`).
- Unroutable events fall back to the most recently cached channel (upstream may
  supersede with session-pinning — decide during rebuild).

### C3 — MCP pre-call delay
- `agent.tool_call_delay_ms > 0` delays only MCP tool calls whose name matches
  `mcp_delay_tool_patterns`; non-MCP tools and unmatched MCP tools are unaffected.

### C4 — Camofox fetch fallback
- `fetchWebContent` registration falls back through camofox stealth-browser rules when
  the plain fetch fails (fortified rule set, 2026-07-21).

### C5 — Model switch keeps main server pinned
- `/model` switch updates the running main server; the switched-from/switched-to main
  server stays excluded from subagent routing.

### C6 — Headless Claude subagent tracking
- `claude -p` subagents launched via the claude-code skill are tracked/pollable
  (tracker polling, watch fix) using `subagent_routing.claude_code.*` defaults.

## Verification checklist (run after rebuild)

Targeted suites (CI-parity wrapper required — do not bare-pytest):
```bash
scripts/run_tests.sh tests/tools/test_subagent_router.py
scripts/run_tests.sh tests/tools/test_subagent_router_deadlock.py
scripts/run_tests.sh tests/tools/test_mcp_tool_delay.py
scripts/run_tests.sh tests/tools/test_process_registry.py
scripts/run_tests.sh tests/cli/test_cli_async_delegation_delivery.py
scripts/run_tests.sh tests/memory/test_mnemosyne.py
scripts/run_tests.sh tests/tools/ tests/gateway/ tests/cli/ tests/tui_gateway/
```

Live E2E on 192.168.1.224 (8 slots, Qwen3.8-PARO-MXFP6):
1. Configure `subagent_routing.priority_order` with 192.168.1.224 @ max_concurrent 8.
2. Fire 10 parallel `delegate_task` leaves → expect 8 running + 2 queued; dispatcher
   drains the queue as slots free; no orphaned slots afterward (active_count returns to 0).
3. Fire a batch with one task carrying `provider: <other-server>` → that task lands on
   the named server only.
4. Kill a mid-run subagent → slot released, queue advances, no dispatcher death.
5. `/model` switch mid-session → main server still excluded from pools.
6. Long-running background delegation → completion notification arrives exactly once,
   with correct provider label, in the originating session.

## Post-merge drop candidates (decide during rebuild; record decision here)

| Item | Upstream supersedes with | Decision |
|---|---|---|
| CLI `_async_delegation_watcher` + `_idle_completion_poller` threads | idle-hook/post-turn drains in `hermes_cli/cli_process_notifications.py` + TUI runtime mixins | PENDING |
| `gateway/_get_fallback_session_source` (fail-open routing) | `_resolve_async_delegation_session` session-pinning (fail-closed) in `gateway/run_notifications.py` | PENDING |
| `give_up_completion_delivery` + attempts=50 | `_MAX_DELIVERY_ATTEMPTS=8` → terminal `dropped` + `defer_completion_delivery` | PENDING |
| Per-event platform/chat_id/chat_type capture | `_ROUTING_KEYS` origin capture at dispatch time | PENDING (keep only if cross-surface routing needs explicit fields) |
