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

Run: `python3 ~/.hermes/skills/bookmarks/scripts/bookmarks.py add <url> [--description "text"] [--tags t1,t2] [--notes "..."]`

This stores the bookmark in Mnemosyne's "bookmarks" memory bank with semantic embedding from the GPU endpoint.

### 2. Search bookmarks

Run: `python3 ~/.hermes/skills/bookmarks/scripts/bookmarks.py search "<query>" [--limit 5]`

Uses Mnemosyne's hybrid search (vector + FTS5 + importance).

### 3. List bookmarks

Run: `python3 ~/.hermes/skills/bookmarks/scripts/bookmarks.py list [--tag FILTER]`

### 4. Show bookmark details

Run: `python3 ~/.hermes/skills/bookmarks/scripts/bookmarks.py show <id_or_url>`

### 5. Delete a bookmark

Run: `python3 ~/.hermes/skills/bookmarks/scripts/bookmarks.py delete <id>`

### 6. List all tags

Run: `python3 ~/.hermes/skills/bookmarks/scripts/bookmarks.py tags`

## Pitfalls
- URL must be valid (scheme + netloc) — the script validates this
- Mnemosyne embedding endpoint must be reachable (192.168.1.225:5679)
- Use the bookmarks memory bank to avoid polluting session context with URLs
- Mnemosyne recall returns dicts, not objects — access via `.get("text")` / `.get("score")`
