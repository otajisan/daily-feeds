"""スコアキャッシュ(data/state.json)の読み書きと期限切れの掃除。"""

import json
from datetime import datetime, timedelta
from pathlib import Path


def load_state(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("items", {})
    return state


def save_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def prune_state(state: dict, cutoff: datetime) -> None:
    horizon = (cutoff - timedelta(days=30)).isoformat()
    state["items"] = {
        k: v for k, v in state["items"].items() if v.get("first_seen", horizon) >= horizon
    }
