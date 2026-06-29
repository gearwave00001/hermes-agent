#!/home/agent/.hermes/hermes-agent/venv/bin/python
"""
bookmarks.py — CLI for URL bookmarking via Mnemosyne memory backend.

Uses a dedicated 'bookmarks' memory bank to keep URLs separate from
session memories. Hybrid search (vector + FTS5 + importance) powered
by Qwen3-Embedding-8B-FP8-DYNAMIC via vLLM on 192.168.1.225:5679.

Usage:
    bookmarks add <url> [--description "text"] [--tags t1,t2] [--notes "..."]
    bookmarks search "<query>" [--limit 5]
    bookmarks list [--tag FILTER]
    bookmarks show <id_or_url>
    bookmarks delete <id>
    bookmarks tags
"""

import argparse
import json
import os
import sys
from urllib.parse import urlparse

# Ensure Mnemosyne uses the GPU embedding endpoint
os.environ.setdefault('MNEMOSYNE_EMBEDDING_API_URL', 'http://192.168.1.225:5679/v1')
os.environ.setdefault('MNEMOSYNE_EMBEDDING_MODEL', 'Qwen3-Embedding-8B-FP8-DYNAMIC')
os.environ.setdefault('MNEMOSYNE_EMBEDDING_DIM', '4096')

from mnemosyne import Mnemosyne
from mnemosyne.core.banks import BankManager


# ── Config ────────────────────────────────────────────────────────────────────

BANK_NAME = "bookmarks"


def get_bookmarks_mem():
    """Get Mnemosyne instance for the bookmarks bank."""
    try:
        BankManager().create_bank(BANK_NAME)
    except ValueError:
        pass  # Bank already exists
    return Mnemosyne(bank=BANK_NAME)


def parse_bookmark_text(text):
    """Parse structured bookmark text back into fields."""
    result = {"url": "", "description": "", "tags": "", "notes": ""}
    for part in text.split(" | "):
        if part.startswith("URL: "):
            result["url"] = part[5:]
        elif part.startswith("Description: "):
            result["description"] = part[13:]
        elif part.startswith("Tags: "):
            result["tags"] = part[6:]
        elif part.startswith("Notes: "):
            result["notes"] = part[7:]
    return result


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_add(args):
    """Add a URL bookmark."""
    url = args.url
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        print(f"Invalid URL: {url}")
        sys.exit(1)

    desc = args.description or ""
    tags = args.tags or ""
    notes = args.notes or ""

    # Build the memory text — structured format for FTS5 + semantic search
    text_parts = [f"URL: {url}"]
    if desc:
        text_parts.append(f"Description: {desc}")
    if tags:
        text_parts.append(f"Tags: {tags}")
    if notes:
        text_parts.append(f"Notes: {notes}")
    memory_text = " | ".join(text_parts)

    mem = get_bookmarks_mem()
    result = mem.remember(
        memory_text,
        importance=0.8,
        source="bookmarks",
        extract_entities=bool(tags or desc),
    )

    print(f"Added bookmark")
    print(f"   URL:    {url}")
    if desc:
        truncated = desc[:80] + ('...' if len(desc) > 80 else '')
        print(f"   Desc:   {truncated}")
    if tags:
        print(f"   Tags:   {tags}")
    if notes:
        truncated = notes[:80] + ('...' if len(notes) > 80 else '')
        print(f"   Notes:  {truncated}")
    if result:
        print(f"   ID:     {result}")


def cmd_search(args):
    """Semantic search bookmarks."""
    query = args.query
    limit = args.limit or 5

    mem = get_bookmarks_mem()
    results = mem.recall(query, top_k=limit)

    if not results:
        print("No matching bookmarks found.")
        return

    print(f"\nFound {len(results)} result(s) (showing top {min(len(results), limit)}):\n")
    for i, result in enumerate(results[:limit], 1):
        # Mnemosyne recall returns dicts
        if isinstance(result, dict):
            text = result.get("text", result.get("content", ""))
            score = result.get("score", 0)
        else:
            text = getattr(result, "text", str(result))
            score = getattr(result, "score", 0)

        fields = parse_bookmark_text(text)
        url = fields["url"]
        desc = fields["description"]
        tags = fields["tags"]

        print(f"  {i}. [{score:.3f}] {url}")
        if desc:
            print(f"     {desc[:100]}")
        if tags:
            print(f"     Tags: {tags}")
        print()


def cmd_list(args):
    """List all bookmarks, optionally filtered by tag."""
    tag_filter = args.tag or ""
    mem = get_bookmarks_mem()

    # Recall with broad query that matches the "URL: " prefix in stored text
    results = mem.recall("URL", top_k=100)

    if not results:
        print(f"No bookmarks found." + (f" with tag '{tag_filter}'" if tag_filter else ""))
        return

    if tag_filter:
        filtered = []
        for r in results:
            text = r.get("text", r.get("content", "")) if isinstance(r, dict) else str(r)
            if tag_filter.lower() in text.lower():
                filtered.append(r)
        results = filtered

    if not results:
        print(f"No bookmarks found" + (f" with tag '{tag_filter}'" if tag_filter else ""))
        return

    print(f"\n{len(results)} bookmark(s):\n")
    for result in results:
        text = result.get("text", result.get("content", "")) if isinstance(result, dict) else str(result)
        fields = parse_bookmark_text(text)
        url = fields["url"]
        desc = fields["description"]
        tags = fields["tags"]

        print(f"  {url}")
        if desc:
            print(f"    {desc[:120]}")
        if tags:
            print(f"    Tags: {tags}")
        print()


def cmd_show(args):
    """Show bookmark details."""
    query = args.id_or_url
    mem = get_bookmarks_mem()

    # Search for the bookmark
    results = mem.recall(query, top_k=5)

    if not results:
        print(f"Bookmark not found: {query}")
        sys.exit(1)

    # Show the best match
    result = results[0]
    text = result.get("text", result.get("content", "")) if isinstance(result, dict) else str(result)
    fields = parse_bookmark_text(text)

    print(f"\nBookmark details:\n")
    if fields["url"]:
        print(f"   URL:     {fields['url']}")
    if fields["description"]:
        print(f"   Desc:    {fields['description']}")
    if fields["notes"]:
        print(f"   Notes:   {fields['notes']}")
    if fields["tags"]:
        print(f"   Tags:    {fields['tags']}")
    print()


def cmd_delete(args):
    """Delete a bookmark."""
    # Mnemosyne uses semantic forgetting or ID-based deletion
    print(f"Note: Mnemosyne uses semantic forgetting. Use one of:")
    print(f"  mnemosyne delete <id>          # if you know the Mnemosyne ID")
    print(f"  mnemosyne forget '<url>'       # semantic deletion by content")
    print()
    print(f"To find the ID, search first:")
    print(f"  bookmarks search '{args.id}'")


def cmd_tags(args):
    """List all unique tags."""
    mem = get_bookmarks_mem()
    results = mem.recall("Tags:", top_k=100)

    tag_counts = {}
    for result in results:
        text = result.get("text", result.get("content", "")) if isinstance(result, dict) else str(result)
        for part in text.split(" | "):
            if part.startswith("Tags: "):
                for tag in part[6:].split(","):
                    tag = tag.strip()
                    if tag:
                        tag_counts[tag] = tag_counts.get(tag, 0) + 1

    if not tag_counts:
        print("No tags found.")
        return

    print(f"\nTags ({len(tag_counts)} unique):\n")
    for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
        print(f"  {tag}: {count}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Track and semantically search bookmarks via Mnemosyne"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Add
    add_parser = subparsers.add_parser("add", help="Add a bookmark URL")
    add_parser.add_argument("url", help="URL to bookmark")
    add_parser.add_argument("--description", "-d", help="Description")
    add_parser.add_argument("--tags", "-t", help="Comma-separated tags")
    add_parser.add_argument("--notes", "-n", help="Additional notes")

    # Search
    search_parser = subparsers.add_parser("search", help="Semantic search")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--limit", "-l", type=int, default=5, help="Max results")

    # List
    list_parser = subparsers.add_parser("list", help="List bookmarks")
    list_parser.add_argument("--tag", help="Filter by tag")

    # Show
    show_parser = subparsers.add_parser("show", help="Show bookmark details")
    show_parser.add_argument("id_or_url", help="Bookmark ID or URL")

    # Delete
    delete_parser = subparsers.add_parser("delete", help="Delete bookmark")
    delete_parser.add_argument("id", help="Bookmark ID or search term")

    # Tags
    subparsers.add_parser("tags", help="List all tags")

    args = parser.parse_args()

    commands = {
        "add": cmd_add,
        "search": cmd_search,
        "list": cmd_list,
        "show": cmd_show,
        "delete": cmd_delete,
        "tags": cmd_tags,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
