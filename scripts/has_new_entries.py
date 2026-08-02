import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


RSS_URL = os.environ.get("BLOG_RSS_URL", "https://hero-news.com/feed")
STATE_PATHS = [
    Path(path.strip())
    for path in os.environ.get("STATE_PATHS", ".bluesky-posted.json,.threads-posted.json").split(",")
    if path.strip()
]
REQUEST_TIMEOUT = int(os.environ.get("RSS_PRECHECK_TIMEOUT", "30"))


def set_output(name, value):
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"{name}={value}\n")


def finish(has_new, reason, count=0):
    set_output("has_new", "true" if has_new else "false")
    set_output("new_count", str(count))
    print(f"RSS precheck: has_new={has_new} count={count} reason={reason}")
    return 0


def load_posted(path):
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as state_file:
        state = json.load(state_file)
    return set(state.get("posted", []))


def text_or_empty(element):
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def parse_entry_ids(feed_bytes):
    root = ET.fromstring(feed_bytes)
    ids = []

    for item in root.findall(".//item"):
        entry_id = text_or_empty(item.find("guid")) or text_or_empty(item.find("link"))
        if entry_id:
            ids.append(entry_id)

    atom_ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.findall(f".//{atom_ns}entry"):
        entry_id = text_or_empty(entry.find(f"{atom_ns}id"))
        if not entry_id:
            link = entry.find(f"{atom_ns}link")
            entry_id = "" if link is None else (link.attrib.get("href") or "").strip()
        if entry_id:
            ids.append(entry_id)

    return ids


def fetch_feed():
    request = urllib.request.Request(
        RSS_URL,
        headers={"User-Agent": "hero-news-auto-post/1.0"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read()


def main():
    posted_sets = []
    for path in STATE_PATHS:
        try:
            posted = load_posted(path)
        except Exception as exc:
            print(f"Could not read {path}: {exc}", file=sys.stderr)
            return finish(True, f"state file read failed: {path}")
        if posted is None:
            return finish(True, f"state file missing: {path}")
        posted_sets.append(posted)

    if not posted_sets:
        return finish(True, "no state files configured")

    try:
        entry_ids = parse_entry_ids(fetch_feed())
    except Exception as exc:
        print(f"RSS precheck failed: {exc}", file=sys.stderr)
        return finish(False, "feed unavailable or unparsable")

    if not entry_ids:
        return finish(False, "no feed entries")

    new_count = 0
    for entry_id in entry_ids:
        if any(entry_id not in posted for posted in posted_sets):
            new_count += 1

    if new_count == 0:
        return finish(False, "all feed entries already posted")

    return finish(True, "new entries found", new_count)


if __name__ == "__main__":
    raise SystemExit(main())
