"""Japanese Bluesky tags using the Telegram title-matching rules."""
import re
from atproto import client_utils


def article_tags(title):
    rules = (
        (r"ウルトラマン|ウルトラセブン|\bUltraman\b", "ウルトラマン"),
        (r"仮面ライダー|\bKamen\s*Rider\b", "仮面ライダー"),
        (r"スーパー戦隊|戦隊|\bSuper\s*Sentai\b", "スーパー戦隊"),
        (r"ゴジラ|\bGodzilla\b", "ゴジラ"),
        (r"ガメラ|\bGamera\b", "ガメラ"),
    )
    # JavaScript word boundaries are ASCII; Japanese adjacent to English still matches.
    js_space = r"[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]"
    tags = [tag for pattern, tag in rules
            if re.search(pattern.replace(r"\s", js_space), title, re.I | re.ASCII)]
    return (["特撮"] + tags)[:4] if tags else []


def build_post(title, link, prefix):
    tags = article_tags(title)
    tag_line = " ".join("#" + tag for tag in tags)
    suffix = ("\n" + tag_line if tags else "") + "\n\n記事を読む"
    base = (prefix + title).strip()
    # Code-point counting is conservative for Bluesky's grapheme limit.
    limit = 300 - len(suffix)
    if len(base) > limit:
        base = base[:limit - 3].rstrip() + "..."
    text = client_utils.TextBuilder().text(base)
    if tags:
        text.text("\n")
        for index, tag in enumerate(tags):
            if index:
                text.text(" ")
            text.tag("#" + tag, tag)
    return text.text("\n\n").link("記事を読む", link)
