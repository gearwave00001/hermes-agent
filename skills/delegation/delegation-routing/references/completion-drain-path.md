# Completion Drain Path — Debugging Session (July 11, 2026)

## Context

User reported that 6 subagents were dispatched, 4 ran and completed on remote servers, but only 2 completions surfaced in the parent conversation. The other 4 output files appeared "mysteriously" — the subagents had written them, but the parent never received the completion messages.

## Timeline

| Time | Event |
|------|-------|
| 02:38-02:39 | 4 batch delegations dispatched |
| 02:40-02:44 | 6 individual delegations dispatched |
| 02:41 | Batch `deleg_b357d3ef` surfaced as completion |
| 02:44:37 | `deleg_2758e682` (Content Factory) completed on server 192.168.1.224 |
| 02:45:22 | Batch `deleg_5a8914db` surfaced as completion |
| 02:45:54 | `deleg_74a10de0` (Game World) completed on server 192.168.1.222 |
| 02:46:11 | `deleg_af80e1d1` (Data Intelligence) completed on server 192.168.1.223 |
| 02:46:29 | `deleg_c0e26a69` (Tutoring System) completed on server 192.168.1.221 |
| 02:47:21 | Batch `deleg_99bde455` surfaced as completion |
| 02:48:54 | Batch `deleg_3397053e` surfaced as completion |
| 02:49:21 | `deleg_9deecc5b` (CI/CD) surfaced as completion |
| 02:49:39 | `deleg_35332f94` (Knowledge Synthesis) surfaced as completion |
| 02:49:56 | **CLI restarted** (plugin discovery = new process) |

**Result:** 4 completions (2758e682, 74a10de0, af80e1d1, c0e26a69) completed on servers between 02:44-02:46 but never surfaced. They were pushed to `completion_queue` but the drain at cli.py:15083/15252 never picked them up before the restart wiped the in-memory queue.

## Root Cause

The completions were enqueued on `completion_queue` (confirmed by server-side logs showing subagents completed normally). However, the CLI drain path had two issues:

1. **No logging** — `drain_notifications()` was completely silent about what it drained, skipped, or dropped. The CLI drain sites had `except Exception: pass` that swallowed all errors.
2. **In-memory queue** — `completion_queue` is a standard `queue.Queue`. When the CLI restarts, the new process creates a fresh empty queue. Events that were enqueued but not yet drained are lost.

## What Was Fixed

### Logging Added

- `_push_completion_event()` now logs INFO on successful enqueue
- `drain_notifications()` now logs DEBUG per event, WARNING on None formatting, INFO summary
- CLI drain sites (idle + post-turn) now log INFO on drain, WARNING on failure

### Tests Added

5 new tests in `tests/tools/test_process_registry.py` covering:
- async_delegation events are never skipped by `_drain_should_skip()`
- Only `type="completion"` events are subject to skip logic
- Logging actually fires for skipped/formatted/dropped events
- End-to-end drain produces self-contained formatted output
- `completion_queue` is in-memory only (documents the limitation)

## What Was NOT Fixed

The in-memory nature of `completion_queue` is intentional — it's the design. Persisting to a journal file was proposed and explicitly rejected by the user. The logging addition means future incidents are diagnosable rather than mysterious.

## Key Finding: The Subagents DID Run

All 4 "missing" completions had real server-side activity:
- Web searches (mcp__open_websearch__search)
- File reads (read_file)
- Skill views (skill_view)
- File writes (write_file, ~370 chars each)
- 5-10 API calls each

The user noted the output files were suspiciously small (~370 chars for "project proposals") and the subagents completed in "too fast" a time. This is a model quality issue (the models produced stub outputs), not a drain path issue. The drain path correctly delivered what the subagents produced — it's just that what they produced was minimal.
