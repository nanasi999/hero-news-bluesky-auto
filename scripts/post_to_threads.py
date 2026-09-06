import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

from blog_entries import fetch_homepage_entries
from test_runtime import require_runtime
from source_entries import collect
from log_safety import diagnostic


RSS_URL = os.environ.get("BLOG_RSS_URL", "https://example.invalid/feed")
STATE_PATH = Path(os.environ.get("STATE_PATH", ".threads-posted.json"))
MAX_POSTS = int(os.environ.get("MAX_POSTS", "5"))
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}
THREADS_GRAPH_BASE = os.environ.get("THREADS_GRAPH_BASE", "https://graph.threads.net/v1.0")
POST_LIMIT = int(os.environ.get("THREADS_POST_LIMIT", "500"))
PUBLISH_ATTEMPTS = int(os.environ.get("THREADS_PUBLISH_ATTEMPTS", "6"))
PUBLISH_RETRY_SECONDS = int(os.environ.get("THREADS_PUBLISH_RETRY_SECONDS", "10"))


def load_state():
    if not STATE_PATH.exists():
        return None
    with STATE_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def get_entry_identifiers(entry):
    seen = set()
    identifiers = []
    for value in (entry.get("id"), entry.get("guid"), entry.get("link")):
        value = (value or "").strip()
        if value and value not in seen:
            identifiers.append(value)
            seen.add(value)
    return tuple(identifiers)


def get_entry_id(entry):
    identifiers = get_entry_identifiers(entry)
    return identifiers[0] if identifiers else None


def get_entry_date(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def build_post(title, link):
    title = (title or "New article").strip()
    link = link.strip()
    text = f"{title}\n{link}".strip()
    if len(text) <= POST_LIMIT:
        return text

    title_limit = POST_LIMIT - len(link) - 1
    if title_limit <= 0:
        return link[:POST_LIMIT]
    if title_limit <= 3:
        return f"{title[:title_limit]}\n{link}"[:POST_LIMIT]
    return f"{title[: title_limit - 3].rstrip()}...\n{link}"


def main(runtime=None):
    if not DRY_RUN:
        require_runtime(runtime)
    entries = collect(feedparser.parse, fetch_homepage_entries, RSS_URL)
    if not entries:
        print("No new entries in valid feed.")
        return 0

    current_ids = {
        identifier
        for entry in entries
        for identifier in get_entry_identifiers(entry)
    }

    state = load_state()
    if state is None:
        state = {
            "posted": sorted(current_ids),
            "initialized_at": datetime.now(timezone.utc).isoformat(),
        }
        if DRY_RUN:
            print(f"[DRY_RUN] Would initialize state with {len(current_ids)} existing entries. No posts sent.")
        else:
            save_state(state)
            print(f"Initialized state with {len(current_ids)} existing entries. No posts sent.")
        return 0

    posted = set(state.get("posted", []))
    candidates = [
        entry
        for entry in entries
        if not any(
            identifier in posted
            for identifier in get_entry_identifiers(entry)
        )
    ]

    if not candidates:
        print("No new entries.")
        return 0

    candidates.sort(key=get_entry_date)
    targets = candidates[:MAX_POSTS]

    failed = False

    for entry in targets:
        title = entry.get("title", "New article").strip()
        link = entry["link"]
        text = build_post(title, link)
        entry_id = get_entry_id(entry)

        if DRY_RUN:
            print(f"[DRY_RUN] Would post: {text}")
            continue

        try:
            runtime.post("threads", get_entry_identifiers(entry), {"text": text}, posted)
            posted.update(get_entry_identifiers(entry))
            state["posted"] = sorted(posted)
            save_state(state)
        except Exception as exc:
            failed = True
            print(diagnostic(exc), file=sys.stderr)

    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())

