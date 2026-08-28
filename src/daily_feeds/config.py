"""feeds.yml の読み込みと既定値の補完。"""

from pathlib import Path

import yaml


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
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
