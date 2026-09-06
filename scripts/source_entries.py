"""Classify usable fallback data separately from failed acquisition."""
from log_safety import SafeFailure


def collect(parse, homepage, url):
    entries = []
    valid_empty = False
    try:
        feed = parse(url)
        status = getattr(feed, "status", 200)
        if isinstance(status, int) and status < 400:
            entries = [entry for entry in feed.entries if entry.get("link")]
            valid_empty = bool(getattr(feed, "version", "")) and not feed.bozo
    except Exception:
        print("RSS acquisition failed; trying fallback.")
    known = {entry["link"] for entry in entries}
    try:
        for entry in homepage(url):
            if entry.get("link") and entry["link"] not in known:
                entries.append(entry)
                known.add(entry["link"])
    except Exception:
        print("Homepage acquisition failed.")
    if not entries and not valid_empty:
        raise SafeFailure("No usable source entries.")
    return entries

