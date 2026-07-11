---
name: bookmarks
description: "Track, categorize, and semantically search URLs using Mnemosyne memory backend."
---

# Bookmarks — URL Tracking with Mnemosyne

## Trigger conditions
- User wants to save a URL for later reference
- User wants to search previously saved bookmarks
- User mentions "bookmark", "save this link", "remember this URL"

## Steps

### 1. Add a bookmark

Run: `bookmarks add <url> [--description "text"] [--tags t1,t2] [--notes "..."]`

Stores the bookmark in Mnemosyne's "bookmarks" memory bank with semantic embedding from the GPU endpoint (192.168.1.225:5679).

### 2. Search bookmarks

Run: `bookmarks search "<query>" [--limit 5]`

Uses Mnemosyne's hybrid search (vector + FTS5 + importance scoring).

### 3. List bookmarks

Run: `bookmarks list [--tag FILTER]`

Uses broad "URL" recall query internally (not "bookmarks" — see Pitfalls).

### 4. Show bookmark details

Run: `bookmarks show <id_or_url>`

### 5. Delete a bookmark

Run: `bookmarks delete <id_or_url>`

Note: Mnemosyne uses semantic forgetting. The script delegates to `mnemosyne delete <id>` or `mnemosyne forget '<url>'`.

### 6. List all tags

Run: `bookmarks tags`

## Pitfalls

- **URL must be valid** (scheme + netloc) — the script validates this
- **Bank creation is not idempotent** — `BankManager().create_bank()` raises `ValueError` if the bank exists. Always wrap in try/except.
- **Mnemosyne recall returns dicts, not objects** — access via `.get("text")` / `.get("score")`, not `.text` / `.score`
- **List query must be "URL", not "bookmarks"** — stored text uses "URL: ..." prefix, so `recall("URL")` finds all bookmarks while `recall("bookmarks")` returns 0 results
- **Shebang must use venv Python** — the script shebang points to `~/.hermes/hermes-agent/venv/bin/python`, not `/usr/bin/env python3` (system Python lacks mnemosyne)
- **`MNEMOSYNE_EMBEDDING_DIM=4096` is required** — Qwen3-Embedding-8B produces 4096-dim vectors. Mnemosyne defaults to 384 (bge-small). Without this env var, vector inserts fail with dimension mismatch.
- **Mnemosyne plugin must be installed separately** — `pip install mnemosyne-hermes` installs the package, but you must also run `python -m mnemosyne_hermes.install` to create the plugin symlink at `~/.hermes/plugins/mnemosyne`. Without this, `hermes memory status` shows "Plugin: NOT installed" and no embedding traffic occurs during conversation.
- **`memory.provider: mnemosyne` may reset on sandbox restart** — verify with `hermes memory status` after restart. `memory_enabled: false` tends to survive; `provider` does not.
- **Bookmarks don't auto-expire** — stored with `importance=0.8`, no `valid_until`. They age via recency decay (default 1 week halflife) but persist forever in the DB.
- **Recency halflife is global** — `MNEMOSYNE_RECENCY_HALFLIFE` applies to all banks. Not per-bank. Default 168h (1 week). Set higher (e.g., 720h = 30 days) if bookmarks should stay discoverable longer.

## Setup reference

See `references/setup.md` for Mnemosyne installation, configuration, and troubleshooting.

## Linked files

- `scripts/bookmarks.py` — CLI wrapper around Mnemosyne SDK
- `references/setup.md` — Mnemosyne installation and configuration guide
