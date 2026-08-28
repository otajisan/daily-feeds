"""コマンドラインからの集約実行。

feeds.yml を読んでフィードを集約し、新着分だけGeminiでスコアリングして
GitHub Pages用の feed.xml / feed-ranked.xml / feed.json を生成する。

- スコア済み記事は data/state.json にキャッシュし、新着分だけGeminiに投げる
- GEMINI_API_KEY が無い/呼び出しに失敗した場合はスコア50でフォールバックし、
  集約自体は必ず成功させる(失敗分はキャッシュせず次回再挑戦する)

入出力のパスは引数で受け取る。既定値はカレントディレクトリからの相対パスなので、
リポジトリのルートで実行することを前提にしている。
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daily_feeds.config import load_config
from daily_feeds.fetching import fetch_all
from daily_feeds.output import build_json, build_rss
from daily_feeds.scoring import DEFAULT_SCORE, score_new_items
from daily_feeds.state import load_state, prune_state, save_state

DEFAULT_CONFIG_PATH = Path("feeds.yml")
DEFAULT_STATE_PATH = Path("data/state.json")
DEFAULT_DOCS_DIR = Path("docs")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Aggregate and score RSS/Atom feeds.")
    p.add_argument("--dry-run", action="store_true", help="docs/ と data/state.json を書き換えない")
    p.add_argument(
        "--no-gemini", action="store_true", help="Gemini呼び出しをせず全件フォールバックスコアにする"
    )
    p.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH, help="フィード定義YAML (既定: feeds.yml)"
    )
    p.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help="スコアキャッシュJSON (既定: data/state.json)",
    )
    p.add_argument(
        "--docs-dir", type=Path, default=DEFAULT_DOCS_DIR, help="生成物の出力先 (既定: docs)"
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    cfg = load_config(args.config)
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

    state = load_state(args.state)
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

    args.docs_dir.mkdir(parents=True, exist_ok=True)
    build_rss(by_date, cfg, args.docs_dir / "feed.xml", "", show_score=False)
    build_rss(by_score, cfg, args.docs_dir / "feed-ranked.xml", " (Ranked)", show_score=True)
    build_json(for_json, cfg, args.docs_dir / "feed.json")

    prune_state(state, cutoff)
    save_state(state, args.state)


if __name__ == "__main__":
    main()
