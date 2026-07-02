import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests


RSS_URL = os.environ.get("BLOG_RSS_URL", "https://hero-news.com/feed")
STATE_PATH = Path(os.environ.get("STATE_PATH", ".threads-posted.json"))
MAX_POSTS = int(os.environ.get("MAX_POSTS", "5"))
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}
THREADS_GRAPH_BASE = os.environ.get("THREADS_GRAPH_BASE", "https://graph.threads.net/v1.0")
POST_LIMIT = int(os.environ.get("THREADS_POST_LIMIT", "500"))


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


def post_to_threads(user_id, access_token, text):
    create_url = f"{THREADS_GRAPH_BASE}/{user_id}/threads"
    publish_url = f"{THREADS_GRAPH_BASE}/{user_id}/threads_publish"

    create_response = requests.post(
        create_url,
        data={
            "media_type": "TEXT",
            "text": text,
            "access_token": access_token,
        },
        timeout=30,
    )
    if not create_response.ok:
        raise RuntimeError(f"Create Threads post failed: {create_response.status_code} {create_response.text}")

    creation_id = create_response.json().get("id")
    if not creation_id:
        raise RuntimeError(f"Create Threads post response did not include id: {create_response.text}")

    publish_response = requests.post(
        publish_url,
        data={
            "creation_id": creation_id,
            "access_token": access_token,
        },
        timeout=30,
    )
    if not publish_response.ok:
        raise RuntimeError(f"Publish Threads post failed: {publish_response.status_code} {publish_response.text}")

    return publish_response.json()


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
        save_state(
            {
                "posted": sorted(current_ids),
                "initialized_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        print(f"Initialized state with {len(current_ids)} existing entries. No posts sent.")
        return 0

    posted = set(state.get("posted", []))
    candidates = [entry for entry in entries if get_entry_id(entry) not in posted]

    if not candidates:
        print("No new entries.")
        return 0

    candidates.sort(key=get_entry_date)
    targets = candidates[:MAX_POSTS]

    user_id = os.environ.get("THREADS_USER_ID")
    access_token = os.environ.get("THREADS_ACCESS_TOKEN")
    if not DRY_RUN and (not user_id or not access_token):
        raise RuntimeError("THREADS_USER_ID and THREADS_ACCESS_TOKEN are required.")

    for entry in targets:
        title = entry.get("title", "New article").strip()
        link = entry["link"]
        text = build_post(title, link)

        if DRY_RUN:
            print(f"[DRY_RUN] Would post: {text}")
        else:
            result = post_to_threads(user_id, access_token, text)
            print(f"Posted: {title} -> {result}")

        posted.add(get_entry_id(entry))

    state["posted"] = sorted(posted)
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
