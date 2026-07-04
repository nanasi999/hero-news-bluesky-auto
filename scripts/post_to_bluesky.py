import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
from atproto import Client, client_utils


RSS_URL = os.environ.get("BLOG_RSS_URL", "https://hero-news.com/feed")
STATE_PATH = Path(os.environ.get("STATE_PATH", ".bluesky-posted.json"))
MAX_POSTS = int(os.environ.get("MAX_POSTS", "5"))
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}
POST_PREFIX = os.environ.get("POST_PREFIX", "新着記事: ")


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


def get_entry_id(entry):
    return entry.get("id") or entry.get("guid") or entry.get("link")


def get_entry_date(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def build_post(title, link):
    base = f"{POST_PREFIX}{title}".strip()
    suffix = "\n読む"
    limit = 300 - len(suffix)
    if len(base) > limit:
        base = base[: limit - 3].rstrip() + "..."

    text = client_utils.TextBuilder()
    text.text(base)
    text.text("\n")
    text.link("読む", link)
    return text


def main():
    feed = feedparser.parse(RSS_URL)
    if feed.bozo:
        print(f"Feed parse warning: {feed.bozo_exception}", file=sys.stderr)

    entries = [entry for entry in feed.entries if entry.get("link")]
    if not entries:
        print("No feed entries found.")
        return 0

    current_ids = {get_entry_id(entry) for entry in entries}
    current_ids.discard(None)

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
    candidates = [entry for entry in entries if get_entry_id(entry) not in posted]

    if not candidates:
        print("No new entries.")
        return 0

    candidates.sort(key=get_entry_date)
    targets = candidates[:MAX_POSTS]

    client = None
    if not DRY_RUN:
        client = Client()
        client.login(os.environ["BLUESKY_HANDLE"], os.environ["BLUESKY_APP_PASSWORD"])

    for entry in targets:
        title = entry.get("title", "New article").strip()
        link = entry["link"]
        entry_id = get_entry_id(entry)

        if DRY_RUN:
            print(f"[DRY_RUN] Would post: {title} {link}")
            continue

        result = client.send_post(build_post(title, link))
        print(f"Posted: {title} -> {result.uri}")

        posted.add(entry_id)
        state["posted"] = sorted(posted)
        save_state(state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
