"""フィードの取得と、エントリから記事dictへの正規化。"""

import calendar
import hashlib
import html
import re
import socket
import sys
import time
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

from daily_feeds.http_status import RETRYABLE_STATUS

FETCH_TIMEOUT_SEC = 20
FETCH_RETRIES = 2
FETCH_BACKOFF_SEC = 3.0

# 追跡用クエリパラメータ(重複排除のキーからは落とす。表示用リンクは元のまま)
TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "spm", "yclid"}

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

socket.setdefaulttimeout(FETCH_TIMEOUT_SEC)


def normalize_url(url: str) -> str:
    """重複排除用にURLを正規化する(追跡パラメータ・フラグメント・末尾スラッシュを除去)。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def item_id(link: str) -> str:
    return hashlib.sha1(normalize_url(link).encode("utf-8")).hexdigest()[:16]


def entry_datetime(entry, now: datetime) -> datetime | None:
    """feedparserの *_parsed はUTCのstruct_timeなので calendar.timegm で変換する。

    time.mktime だとローカルタイムゾーンぶんずれる(JSTなら9時間)。
    """
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            dt = datetime.fromtimestamp(calendar.timegm(t), tz=UTC)
            # 未来日付の記事が上位に居座らないようクランプする
            return min(dt, now)
    return None


def strip_html(text: str, limit: int = 500) -> str:
    """雑にタグを落として要約用テキストにする。"""
    text = TAG_RE.sub(" ", text or "")
    text = html.unescape(text)
    text = WS_RE.sub(" ", text).strip()
    return text[:limit]


def entry_summary(entry) -> str:
    """summary を優先しつつ、内容が薄い場合は content(Atom本文)にフォールバックする。"""
    summary = strip_html(entry.get("summary", ""))
    if len(summary) >= 80:
        return summary
    for c in entry.get("content") or []:
        body = strip_html(c.get("value", ""))
        if len(body) > len(summary):
            summary = body
    return summary


def fetch_feed(url: str, agent: str) -> tuple[object | None, str]:
    """フィードを取得する。取得できなければ (None, 理由)。一時的な失敗はリトライする。"""
    note = ""
    delay = FETCH_BACKOFF_SEC
    for attempt in range(FETCH_RETRIES + 1):
        try:
            parsed = feedparser.parse(url, agent=agent)
        except Exception as e:  # noqa: BLE001
            note = f"{type(e).__name__}: {e}"
        else:
            status = getattr(parsed, "status", None)
            if parsed.entries:
                return parsed, ""
            if status == 200 and not parsed.bozo:
                return parsed, "0 entries"
            if status and 400 <= status < 500 and status not in RETRYABLE_STATUS:
                return None, f"HTTP {status}"
            note = f"HTTP {status}" if status else str(parsed.get("bozo_exception") or "no entries")
        if attempt < FETCH_RETRIES:
            time.sleep(delay)
            delay *= 2
    return None, note or "no entries"


def fetch_all(cfg: dict, now: datetime) -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    failures: list[str] = []
    agent = cfg["settings"]["user_agent"]
    for feed in cfg.get("feeds", []):
        url = feed["url"]
        name = feed.get("name", url)
        parsed, note = fetch_feed(url, agent)
        if parsed is None:
            print(f"[warn] fetch failed: {name}: {note}", file=sys.stderr)
            failures.append(name)
            continue
        for entry in parsed.entries:
            link = entry.get("link")
            if not link:
                continue
            items.append(
                {
                    "id": item_id(link),
                    "title": (entry.get("title") or "(no title)").strip(),
                    "link": link,
                    "summary": entry_summary(entry),
                    "published": (entry_datetime(entry, now) or now).isoformat(),
                    "source": name,
                    "category": feed.get("category", ""),
                }
            )
        suffix = f" ({note})" if note else ""
        print(f"[info] {name}: {len(parsed.entries)} entries{suffix}")
    return items, failures
