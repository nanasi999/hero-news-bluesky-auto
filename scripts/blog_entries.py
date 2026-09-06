import html
import os
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from html.parser import HTMLParser


REQUEST_TIMEOUT = int(os.environ.get("BLOG_SOURCE_TIMEOUT", "30"))
HOMEPAGE_MAX_AGE_DAYS = int(os.environ.get("BLOG_HOMEPAGE_MAX_AGE_DAYS", "2"))


def _site_origin(rss_url):
    parsed = urllib.parse.urlsplit(rss_url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _request(url):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html",
            "Cache-Control": "no-cache",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/140.0 Safari/537.36"
            ),
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read()


def _parse_date(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return time.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return None


class _HomepageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current = None
        self.in_title = False
        self.entries = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "article":
            self.current = {"link": "", "title_parts": [], "date": ""}
            self.in_title = False
            return

        if self.current is None:
            return

        classes = set(attributes.get("class", "").split())
        if tag == "h1" and "article-title" in classes:
            self.in_title = True
        elif tag == "a" and self.in_title and not self.current["link"]:
            self.current["link"] = attributes.get("href", "").strip()
        elif tag == "time" and not self.current["date"]:
            self.current["date"] = attributes.get("datetime", "")[:10]

    def handle_data(self, data):
        if self.current is not None and self.in_title:
            self.current["title_parts"].append(data)

    def handle_endtag(self, tag):
        if tag == "h1":
            self.in_title = False
        elif tag == "article" and self.current is not None:
            self.current["title"] = html.unescape(
                " ".join("".join(self.current["title_parts"]).split())
            )
            if self.current["link"] and self.current["title"] and self.current["date"]:
                self.entries.append(self.current)
            self.current = None
            self.in_title = False


def fetch_homepage_entries(rss_url):
    origin = _site_origin(rss_url)
    parser = _HomepageParser()
    parser.feed(_request(f"{origin}/").decode("utf-8", errors="replace"))

    cutoff = date.today() - timedelta(days=HOMEPAGE_MAX_AGE_DAYS)
    recent = []
    for item in parser.entries:
        try:
            published_date = date.fromisoformat(item["date"])
        except ValueError:
            continue
        if published_date >= cutoff:
            recent.append(item)

    entries = []
    total = len(recent)
    for index, item in enumerate(recent):
        parsed = _parse_date(item["date"])
        if parsed:
            timestamp = time.mktime(parsed) + (total - index)
            parsed = time.gmtime(timestamp)
        entries.append(
            {
                "id": item["link"],
                "guid": item["link"],
                "link": item["link"],
                "title": item["title"],
                "published": item["date"],
                "published_parsed": parsed,
            }
        )

    if not entries:
        raise RuntimeError("Homepage returned no recent posts.")
    return entries
