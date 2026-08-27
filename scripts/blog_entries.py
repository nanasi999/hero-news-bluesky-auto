import html
import json
import os
import re
import time
import urllib.parse
import urllib.request


REQUEST_TIMEOUT = int(os.environ.get("BLOG_SOURCE_TIMEOUT", "30"))
API_ENTRY_LIMIT = int(os.environ.get("BLOG_API_ENTRY_LIMIT", "50"))


def _site_origin(rss_url):
    parsed = urllib.parse.urlsplit(rss_url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _api_url(rss_url):
    configured = os.environ.get("BLOG_API_URL", "").strip()
    if configured:
        return configured

    query = urllib.parse.urlencode(
        {
            "per_page": API_ENTRY_LIMIT,
            "orderby": "date",
            "order": "desc",
            "_fields": "id,link,date_gmt,modified_gmt,title",
        }
    )
    return f"{_site_origin(rss_url)}/wp-json/wp/v2/posts?{query}"


def _clean_title(value):
    rendered = value.get("rendered", "") if isinstance(value, dict) else str(value or "")
    return html.unescape(re.sub(r"<[^>]+>", "", rendered)).strip()


def _parse_date(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def fetch_wordpress_entries(rss_url):
    origin = _site_origin(rss_url)
    request = urllib.request.Request(
        _api_url(rss_url),
        headers={
            "Accept": "application/json",
            "User-Agent": "hero-news-auto-post/2.0",
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        payload = json.load(response)

    if not isinstance(payload, list) or not payload:
        raise RuntimeError("WordPress API returned no posts.")

    entries = []
    for post in payload:
        post_id = post.get("id")
        link = str(post.get("link", "")).strip()
        if not post_id or not link:
            continue

        identifier = f"{origin}/?p={post_id}"
        published = post.get("date_gmt") or post.get("modified_gmt")
        entries.append(
            {
                "id": identifier,
                "guid": identifier,
                "link": link,
                "title": _clean_title(post.get("title")),
                "published": published,
                "published_parsed": _parse_date(published),
            }
        )

    if not entries:
        raise RuntimeError("WordPress API returned no usable posts.")
    return entries
