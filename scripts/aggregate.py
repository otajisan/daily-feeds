#!/usr/bin/env python3
"""RSS/Atomフィードを集約し、新着記事をGeminiでスコアリングして
GitHub Pages用の feed.xml / feed-ranked.xml / feed.json を生成する。

- 購読対象は feeds.yml で管理
- スコア済み記事は data/state.json にキャッシュし、新着分だけGeminiに投げる
- GEMINI_API_KEY が無い/呼び出しに失敗した場合はスコア50でフォールバックし、
  集約自体は必ず成功させる(失敗分はキャッシュせず次回再挑戦する)
"""

import argparse
import calendar
import hashlib
import html
import json
import os
import random
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import feedparser
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "feeds.yml"
STATE_PATH = ROOT / "data" / "state.json"
DOCS_DIR = ROOT / "docs"

DEFAULT_SCORE = 50
FETCH_TIMEOUT_SEC = 20
FETCH_RETRIES = 2
FETCH_BACKOFF_SEC = 3.0
SCORE_RETRIES = 3
SCORE_BACKOFF_SEC = 5.0
SCORE_BATCH_PAUSE_SEC = 2.0
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
RETRYABLE_EXCEPTIONS = {
    "RemoteProtocolError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "ReadError",
    "WriteError",
    "WriteTimeout",
    "PoolTimeout",
    "ProtocolError",
    "IncompleteRead",
    "NetworkError",
    "SSLError",
}
# 入力が同じ限り結果が変わらない終了理由。リトライしても無駄なので即フォールバックする
TERMINAL_FINISH_REASONS = (
    "SAFETY",
    "PROHIBITED_CONTENT",
    "RECITATION",
    "BLOCKLIST",
    "SPII",
    "MAX_TOKENS",
    "MALFORMED_FUNCTION_CALL",
    "OTHER",
    "BLOCKED",
    "NO_CANDIDATES",
)
RETRYABLE_ERROR_HINTS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "RESOURCE_EXHAUSTED",
    "UNAVAILABLE",
    "INTERNAL",
    "DEADLINE_EXCEEDED",
    "Server disconnected",
    "Connection reset",
    "timed out",
)

# 追跡用クエリパラメータ(重複排除のキーからは落とす。表示用リンクは元のまま)
TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "spm", "yclid"}

# XML 1.0 に出力できない文字
INVALID_XML_CHARS = re.compile(
    "[^\\u0009\\u000a\\u000d\\u0020-\\ud7ff\\ue000-\\ufffd\\U00010000-\\U0010ffff]"
)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

socket.setdefaulttimeout(FETCH_TIMEOUT_SEC)


# ---------------------------------------------------------------- config / state

def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("settings", {})
    s = cfg["settings"]
    s.setdefault("retention_days", 14)
    s.setdefault("max_items", 100)
    # feed.json はタイムラインUIのデータ源なので、XMLより多く載せる
    s.setdefault("json_max_items", 400)
    s.setdefault("score_batch_size", 20)
    s.setdefault("gemini_model", "gemini-3.5-flash-lite")
    s.setdefault("gemini_thinking_level", None)
    s.setdefault("site_url", "https://otajisan.github.io/daily-feeds")
    s.setdefault("feed_title", "daily-feeds")
    s.setdefault("feed_description", "Aggregated tech feeds")
    s.setdefault("user_agent", f"daily-feeds/1.0 (+{s['site_url']})")
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("items", {})
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


# ---------------------------------------------------------------- fetch

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
            dt = datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
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


# ---------------------------------------------------------------- scoring (Gemini)

SCORING_PROMPT = """あなたは技術ニュースの編集者です。以下の記事リストについて、
技術者コミュニティ全体にとっての一般的な重要度を0〜100でスコアリングしてください。

高スコアの目安:
- セキュリティ脆弱性・緊急の修正 (80-100)
- 主要なOSS/プラットフォームのメジャーリリース、破壊的変更 (70-95)
- 業界に広く影響するニュース・発表 (60-90)
- 有用な技術解説・ベストプラクティス (40-70)
- マイナーアップデート、ニッチな話題 (10-40)

記事リストの全件について、JSON配列で返してください。
形式: [{"idx": <記事番号>, "score": <0-100の整数>, "reason": "<20字程度の理由>"}]

記事リスト:
"""

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "idx": {"type": "INTEGER"},
            "score": {"type": "INTEGER"},
            "reason": {"type": "STRING"},
        },
        "required": ["idx", "score", "reason"],
    },
}


class TransientScoringError(RuntimeError):
    """空レスポンスなど、リトライで回復しうるスコアリング失敗。"""


def is_retryable(e: Exception) -> bool:
    """一時的な失敗かどうか。HTTPステータスだけでなくコネクション断も拾う。"""
    code = getattr(e, "code", None)
    if isinstance(code, int):
        if code in RETRYABLE_STATUS:
            return True
        if 400 <= code < 500:  # 投げ直しても直らないリクエスト側のエラー
            return False
    if isinstance(e, (TransientScoringError, json.JSONDecodeError, ConnectionError, TimeoutError)):
        return True
    # httpx等のコネクション系例外はライブラリ固有の型なので名前で判定する
    # (例: "Server disconnected without sending a response." = RemoteProtocolError)
    if type(e).__name__ in RETRYABLE_EXCEPTIONS:
        return True
    return any(t in str(e) for t in RETRYABLE_ERROR_HINTS)


def parse_score_response(text: str, batch: list[dict]) -> dict[str, dict]:
    data = json.loads(text)
    if isinstance(data, dict):  # スキーマを無視して包んで返してきた場合の保険
        data = next((v for v in data.values() if isinstance(v, list)), [])
    results = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row["idx"])
            score = max(0, min(100, int(row["score"])))
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < len(batch):
            results[batch[idx]["id"]] = {"score": score, "reason": str(row.get("reason", ""))[:100]}
    return results


def finish_reason(resp) -> str:
    """終了理由を返す。候補が無い場合はプロンプト段階でブロックされたとみなす。"""
    block = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
    if block:
        return f"BLOCKED({block})"
    try:
        return str(resp.candidates[0].finish_reason)
    except Exception:  # noqa: BLE001
        return "NO_CANDIDATES"


def gemini_score_batch(client, cfg: dict, batch: list[dict]) -> dict[str, dict]:
    """1バッチをGeminiでスコアリングする。429/5xxは指数バックオフでリトライする。"""
    s = cfg["settings"]
    lines = [
        f"{i}. [{it['source']}] {it['title']}\n   {it['summary'][:200]}"
        for i, it in enumerate(batch)
    ]
    prompt = SCORING_PROMPT + "\n".join(lines)
    config: dict = {
        "response_mime_type": "application/json",
        "response_schema": RESPONSE_SCHEMA,
    }
    # Gemini 3.x は thinking_budget を受け付けず thinking_level ("low"/"high") を使う。
    # 未指定ならモデル既定にまかせる。
    level = s.get("gemini_thinking_level")
    if level:
        config["thinking_config"] = {"thinking_level": str(level)}

    delay = SCORE_BACKOFF_SEC
    last_exc: Exception | None = None
    for attempt in range(SCORE_RETRIES):
        try:
            resp = client.models.generate_content(
                model=s["gemini_model"], contents=prompt, config=config
            )
            if not resp.text:
                reason = finish_reason(resp)
                if any(t in reason.upper() for t in TERMINAL_FINISH_REASONS):
                    raise ValueError(f"blocked response (finish_reason={reason})")
                raise TransientScoringError(f"empty response (finish_reason={reason})")
            return parse_score_response(resp.text, batch)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt == SCORE_RETRIES - 1 or not is_retryable(e):
                break
            wait = delay + random.uniform(0, 1.0)
            print(f"[warn] gemini retry in {wait:.1f}s: {e}", file=sys.stderr)
            time.sleep(wait)
            delay *= 2
    raise last_exc if last_exc else RuntimeError("gemini scoring failed")


def make_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai

        return genai.Client(api_key=api_key)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] gemini client init failed: {e}", file=sys.stderr)
        return None


def score_new_items(
    items: list[dict], state: dict, cfg: dict, cutoff: datetime, use_gemini: bool
) -> None:
    """stateに無い記事だけGeminiでスコアリングし、state['items']に書き込む。

    保持期間外の古い記事はどうせフィードに載らないためスコアリングしない。
    スコアリングに失敗した記事はキャッシュせず、次回実行で再挑戦する。
    """
    known = state["items"]
    todo = [
        it
        for it in items
        if it["published"] >= cutoff.isoformat()
        and (it["id"] not in known or "score" not in known[it["id"]])
    ]
    if not todo:
        print("[info] no new items to score")
        return

    client = make_client() if use_gemini else None
    if client is None:
        print(f"[info] gemini off: {len(todo)} new items use fallback score {DEFAULT_SCORE}")
        return

    batch_size = cfg["settings"]["score_batch_size"]
    batches = [todo[i : i + batch_size] for i in range(0, len(todo), batch_size)]
    scored = failed = 0
    for i, batch in enumerate(batches):
        try:
            results = gemini_score_batch(client, cfg, batch)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] gemini scoring failed for batch {i}: {e}", file=sys.stderr)
            results = {}
        for it in batch:
            r = results.get(it["id"])
            if r is None:
                failed += 1  # キャッシュせず次回に再挑戦させる
                continue
            entry = known.setdefault(it["id"], {})
            entry.update(r)
            entry.setdefault("first_seen", it["published"])
            scored += 1
        if i < len(batches) - 1:
            time.sleep(SCORE_BATCH_PAUSE_SEC)
    print(f"[info] scored {scored}/{len(todo)} new items in {len(batches)} batches ({failed} fell back)")


def prune_state(state: dict, cutoff: datetime) -> None:
    horizon = (cutoff - timedelta(days=30)).isoformat()
    state["items"] = {
        k: v for k, v in state["items"].items() if v.get("first_seen", horizon) >= horizon
    }


# ---------------------------------------------------------------- output

def xml_text(text: str) -> str:
    return escape(INVALID_XML_CHARS.sub("", text or ""))


def rfc822(iso: str) -> str:
    try:
        return format_datetime(datetime.fromisoformat(iso))
    except ValueError:
        return format_datetime(datetime.now(timezone.utc))


def build_rss(items: list[dict], cfg: dict, path: Path, title_suffix: str, show_score: bool) -> None:
    s = cfg["settings"]
    now = format_datetime(datetime.now(timezone.utc))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{xml_text(s['feed_title'] + title_suffix)}</title>",
        f"<link>{xml_text(s['site_url'])}</link>",
        f"<description>{xml_text(s['feed_description'])}</description>",
        f"<lastBuildDate>{now}</lastBuildDate>",
        f'<atom:link href="{xml_text(s["site_url"] + "/" + path.name)}" rel="self" type="application/rss+xml"/>',
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
    print(f"[info] wrote {path.relative_to(ROOT)} ({len(items)} items)")


def build_json(items: list[dict], cfg: dict, path: Path) -> None:
    payload = {
        "title": cfg["settings"]["feed_title"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    # 2時間ごとにコミットされるため、機械が読むJSONは詰めて書き出す
    dumped = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    path.write_text(dumped, encoding="utf-8")
    print(f"[info] wrote {path.relative_to(ROOT)} ({len(items)} items)")


# ---------------------------------------------------------------- main

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Aggregate and score RSS/Atom feeds.")
    p.add_argument("--dry-run", action="store_true", help="docs/ と data/state.json を書き換えない")
    p.add_argument(
        "--no-gemini", action="store_true", help="Gemini呼び出しをせず全件フォールバックスコアにする"
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config()
    s = cfg["settings"]
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=s["retention_days"])

    items, failures = fetch_all(cfg, now)
    if not items:
        print("[error] no items fetched at all; keeping previous output", file=sys.stderr)
        sys.exit(1)
    if failures:
        print(f"[warn] {len(failures)} feed(s) failed: {', '.join(failures)}", file=sys.stderr)

    # 重複排除(同一linkは最初の1件)
    seen, unique = set(), []
    for it in items:
        if it["id"] not in seen:
            seen.add(it["id"])
            unique.append(it)
    print(f"[info] {len(items)} entries -> {len(unique)} unique")

    state = load_state()
    score_new_items(unique, state, cfg, cutoff, use_gemini=not args.no_gemini)

    # スコアをマージし、保持期間でフィルタ
    merged = []
    for it in unique:
        cached = state["items"].get(it["id"], {})
        it["score"] = cached.get("score", DEFAULT_SCORE)
        it["reason"] = cached.get("reason", "")
        if it["published"] >= cutoff.isoformat():
            merged.append(it)
    print(f"[info] {len(merged)} items within retention ({s['retention_days']}d)")

    newest_first = sorted(merged, key=lambda x: x["published"], reverse=True)
    by_date = newest_first[: s["max_items"]]
    by_score = sorted(merged, key=lambda x: (x["score"], x["published"]), reverse=True)[: s["max_items"]]
    # タイムラインで新着が欠けないよう、JSONは保持期間内をまとめて日付順で出す
    for_json = newest_first[: s["json_max_items"]]

    if args.dry_run:
        print("[info] dry-run: nothing written. top items:")
        for it in by_score[:5]:
            print(f"  {it['score']:3d} [{it['source']}] {it['title'][:60]} ({it['reason']})")
        return

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    build_rss(by_date, cfg, DOCS_DIR / "feed.xml", "", show_score=False)
    build_rss(by_score, cfg, DOCS_DIR / "feed-ranked.xml", " (Ranked)", show_score=True)
    build_json(for_json, cfg, DOCS_DIR / "feed.json")

    prune_state(state, cutoff)
    save_state(state)


if __name__ == "__main__":
    main()
