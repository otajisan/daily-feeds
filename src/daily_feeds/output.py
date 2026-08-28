"""GitHub Pages 用の feed.xml / feed-ranked.xml / feed.json を書き出す。"""

import json
import re
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

# XML 1.0 に出力できない文字
INVALID_XML_CHARS = re.compile(
    "[^\\u0009\\u000a\\u000d\\u0020-\\ud7ff\\ue000-\\ufffd\\U00010000-\\U0010ffff]"
)


def xml_text(text: str) -> str:
    return escape(INVALID_XML_CHARS.sub("", text or ""))


def rfc822(iso: str) -> str:
    try:
        return format_datetime(datetime.fromisoformat(iso))
    except ValueError:
        return format_datetime(datetime.now(UTC))


def build_rss(
    items: list[dict], cfg: dict, path: Path, title_suffix: str, show_score: bool
) -> None:
    s = cfg["settings"]
    now = format_datetime(datetime.now(UTC))
    self_url = xml_text(s["site_url"] + "/" + path.name)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{xml_text(s['feed_title'] + title_suffix)}</title>",
        f"<link>{xml_text(s['site_url'])}</link>",
        f"<description>{xml_text(s['feed_description'])}</description>",
        f"<lastBuildDate>{now}</lastBuildDate>",
        f'<atom:link href="{self_url}" rel="self" type="application/rss+xml"/>',
    ]
    for it in items:
        title = f"[{it['score']}] {it['title']}" if show_score else it["title"]
        desc = f"{it['summary']} — {it['source']} / score: {it['score']} ({it['reason']})"
        parts += [
            "<item>",
            f"<title>{xml_text(title)}</title>",
            f"<link>{xml_text(it['link'])}</link>",
            f'<guid isPermaLink="true">{xml_text(it["link"])}</guid>',
            f"<pubDate>{rfc822(it['published'])}</pubDate>",
            f"<category>{xml_text(it['source'])}</category>",
            f"<description>{xml_text(desc)}</description>",
            "</item>",
        ]
    parts += ["</channel>", "</rss>"]
    path.write_text("\n".join(parts), encoding="utf-8")
    print(f"[info] wrote {path.name} ({len(items)} items)")


def build_json(items: list[dict], cfg: dict, path: Path) -> None:
    payload = {
        "title": cfg["settings"]["feed_title"],
        "generated_at": datetime.now(UTC).isoformat(),
        "items": items,
    }
    # 2時間ごとにコミットされるため、機械が読むJSONは詰めて書き出す
    dumped = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    path.write_text(dumped, encoding="utf-8")
    print(f"[info] wrote {path.name} ({len(items)} items)")
