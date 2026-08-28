"""Geminiによる注目度スコアリングと、そのリトライ判定。"""

import json
import os
import random
import sys
import time
from datetime import datetime

from daily_feeds.http_status import RETRYABLE_STATUS

DEFAULT_SCORE = 50
SCORE_RETRIES = 3
SCORE_BACKOFF_SEC = 5.0
SCORE_BATCH_PAUSE_SEC = 2.0
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
    print(
        f"[info] scored {scored}/{len(todo)} new items in {len(batches)} batches "
        f"({failed} fell back)"
    )
