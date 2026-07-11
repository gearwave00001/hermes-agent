---
name: parallel-research-delegation
description: "Parallel research: dispatch subagents, compile reports."
version: 1.0.0
author: Hermes Agent
tags: [delegation, research, parallel, compilation, file-output]
---

# Parallel Research Delegation

When the user asks you to research a topic by sending out multiple subagents (typically 3-6), each covering a different aspect, use this workflow.

## Workflow Steps

### 1. Plan the split
Identify N distinct aspects of the topic and assign one per subagent. Each aspect should be broad enough for a substantial report but narrow enough to avoid overlap. Examples:
- Science/chemistry, process/steps, styles/categories, advanced/techniques
- History, ingredients, equipment, troubleshooting
- Regional traditions, modern trends, practical guides

### 2. Dispatch in parallel (single batch call)
Call `delegate_task()` N times in one response with independent goals and contexts. Key details:
- **Pass the output path explicitly** in both `goal` and `context` so each subagent writes to its own file
- **Specify the format** — ask for Markdown with clear headings and bullet points
- **Ask each subagent to include its report in the final summary too**, not just the file (this is how the result re-enters the conversation)
- Use `toolsets: ["web", "file"]` for research tasks that need both searching and writing

### 3. Verify files while waiting
Subagents complete asynchronously — don't wait blindly. After dispatching, proactively check the output directory with `ls -la <output_dir>` to confirm all files landed. This is faster than waiting for all completion banners.

### 4. Read all completed reports
Use `read_file()` on each output file to load their content. Batch these reads in a single call when possible.

### 5. Compile the master summary
Write a `COMPILED_REPORT.md` (or similar) that:
- Has a table of contents linking to sections
- Summarizes each subagent's findings concisely (not just repeating them verbatim)
- Includes a file index table showing each report, its size, and topic
- Cross-references related topics across reports

### 6. Final delivery
Report completion with:
- The directory path of all generated files
- A summary table of files (name, size, lines, topic)
- Brief mention of what the compiled report covers

## Brainstorming Review Pattern

When dispatching subagents to review brainstorming files (typically 10 files with 10 ideas each), use this variant:

### Pre-dispatch checklist
1. **Exclude the summary document** — before assigning files, check for and skip any `summary.md`, `SUMMARY.md`, or similar master summary file already present in the target directory. Assign only the category-specific files (e.g., `communications-ideas.md`, `consumer-ideas.md`).
2. **Verify file count matches expected subagent count** — if 10 subagents are dispatched, there should be 10 assignable `.md` files (excluding the summary).

### Subagent goal template
Each subagent receives: the file content, its assigned category name, and a goal to (a) read the ideas in the file, (b) search the web for 3-5 real-world examples/market evidence, (c) choose the TOP 3 best ideas based on feasibility/market demand/subagent fit/innovation, and (d) write a ranked roadmap proposal with implementation phases to `<output_dir>/<category>-roadmap.md`.

### Post-dispatch: cross-category compilation
After all individual proposals are written, read ALL of them (batch `read_file` calls), then produce a **consolidated cross-category roadmap** that includes:
- A table ranking top picks across all categories with scores
- A recommended phased build sequence (P0 first, then P1/P2 parallel tracks, etc.)
- Cross-cutting infrastructure requirements shared by multiple proposals
- Market necessity summary synthesized from each subagent's web search evidence

This cross-category compilation is distinct from a simple merge — it identifies synergies between categories (shared infrastructure, overlapping patterns) and recommends an optimal build order that maximizes code reuse.

### Brainstorming Review with Web Search (enhanced variant)

For larger brainstorming reviews (10+ files) where each subagent needs to research the web for market evidence:

**Pre-dispatch:**
1. **Exclude the summary document** — check for and skip `summary.md`/`SUMMARY.md` in the target directory
2. **Verify file count matches expected subagent count** — 10 files → 10 subagents

**Subagent goal template (with web search):**
Each subagent receives: the file content, its assigned category name, and a goal to (a) read the ideas in the file, (b) search the web using **`mcp__open_websearch__search` with DuckDuckGo** (NOT the default Firecrawl-backed `web_search`), (c) fetch 1-2 relevant URLs via `mcp__open_websearch__fetchWebContent`, (d) assess market necessity and practical value, (e) rate ideas 1-5 stars with justification, and (f) write a review to `<output_dir>/reviews/<category>-review.md` with a "TOP 3 PICKS" section.

**Post-dispatch compilation:**
After all reviews land, read ALL of them (batch `read_file`), then produce a **consolidated cross-category roadmap** that includes:
- Cross-cutting analysis: deduplicated overlapping concepts, synergy pairs, and merged duplicates
- A 5-tier prioritized roadmap (T1 Flagships → T5 Future Enhancements) with timelines and effort estimates
- Decision matrix ranking projects by feasibility, market need, differentiation, and cost
- Recommended implementation order with specific week-by-week sequencing
- Appendix: full rating summary table by source file

**Web search specifics:**
- Use `mcp__open_websearch__search` (DuckDuckGo) for web search — faster and more reliable than Firecrawl-backed `web_search` for this pattern
- Each subagent should do 5+ DuckDuckGo queries and fetch 1-2 URLs via `fetchWebContent`
- Ask subagents to cite specific real-world projects, products, or research papers found

## Pitfalls

- **Don't wait for all banners before acting.** Subagents may complete in any order; proactive file checks let you proceed as soon as all files exist.
- **Exclude the summary document during dispatch.** If a `summary.md` (or similar) already exists in the brainstorming directory, do NOT assign it to a subagent — it would be reviewed instead of a category file. Always list files before dispatch and filter out existing summaries.
- **Ensure output paths are absolute** and include a versioned directory name to avoid collisions across sessions (e.g., `beer-research-2` not just `beer-research`).
- **Ask subagents to write both a file AND include the report in their summary.** The file is the durable artifact; the summary re-enters your context for compilation.
- **Keep aspect boundaries clean** — avoid overlap between subagent topics so the compiled report doesn't repeat itself.
- **Background fan-out is automatic.** Since v2026.7.1 (PR #49734), `delegate_task` batches run in the background by default — the parent turn returns immediately. Results re-enter as one consolidated block when all finish. You don't need to set `background=True` anymore.
- **MoA is NOT subagent delegation.** MoA fans out parallel LLM *calls* (no tools, advisory-only). Subagent delegation fans out full AIAgent *workers* (with tools, terminal, file access). They are orthogonal mechanisms that can compose — a MoA aggregator can itself call `delegate_task`.
- **Queued subagents lose IP visibility in dispatch.** When provider capacity limits cause queuing, the immediate dispatch response doesn't show which server IP was assigned. The first N subagents land on their servers immediately (visible as 192.168.1.221–224), but queued ones are assigned after slots open. To track provider IPs for all subagents, check the async completion banners or the delegation cache files (`~/.hermes/cache/delegation/subagent-summary-*.txt`) after all complete — don't rely solely on the dispatch response for queued tasks.

## Reference Files

See `references/beer-research-example.md` for a concrete example from the beer brewing research session (June 2026).
See `references/moa-and-background-fanout.md` for architecture reference on MoA, background fan-out, and the local subagent router.
