"""スコアキャッシュの読み書きと掃除。"""

import json
from datetime import UTC, datetime

from daily_feeds.state import load_state, prune_state, save_state

CUTOFF = datetime(2026, 1, 15, tzinfo=UTC)


def test_load_state_returns_empty_shape_when_file_absent(tmp_path):
    assert load_state(tmp_path / "state.json") == {"items": {}}


def test_load_state_adds_items_key_to_legacy_file(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"other": 1}), encoding="utf-8")
    state = load_state(p)
    assert state["items"] == {}
    assert state["other"] == 1


def test_save_state_creates_parent_directory(tmp_path):
    p = tmp_path / "nested" / "deep" / "state.json"
    save_state({"items": {"a": {"score": 1}}}, p)
    assert json.loads(p.read_text(encoding="utf-8")) == {"items": {"a": {"score": 1}}}


def test_save_state_round_trips(tmp_path):
    p = tmp_path / "state.json"
    original = {"items": {"b": {"score": 80, "reason": "日本語の理由"}, "a": {"score": 10}}}
    save_state(original, p)
    assert load_state(p) == original


def test_save_state_sorts_keys_to_keep_diffs_small(tmp_path):
    p = tmp_path / "state.json"
    save_state({"items": {"z": {}, "a": {}}}, p)
    text = p.read_text(encoding="utf-8")
    assert text.index('"a"') < text.index('"z"')


def test_save_state_does_not_escape_japanese(tmp_path):
    p = tmp_path / "state.json"
    save_state({"items": {"a": {"reason": "セキュリティ"}}}, p)
    assert "セキュリティ" in p.read_text(encoding="utf-8")


def test_prune_state_drops_entries_older_than_30_days_before_cutoff():
    state = {
        "items": {
            "old": {"first_seen": "2025-12-01T00:00:00+00:00"},
            "recent": {"first_seen": "2026-01-10T00:00:00+00:00"},
        }
    }
    prune_state(state, CUTOFF)
    assert set(state["items"]) == {"recent"}


def test_prune_state_keeps_entries_without_first_seen():
    """first_seen が無い古い形式のエントリを巻き添えで消さない。"""
    state = {"items": {"legacy": {"score": 50}}}
    prune_state(state, CUTOFF)
    assert set(state["items"]) == {"legacy"}


def test_prune_state_boundary_is_inclusive():
    horizon = "2025-12-16T00:00:00+00:00"  # cutoff - 30日
    state = {"items": {"exactly_at_horizon": {"first_seen": horizon}}}
    prune_state(state, CUTOFF)
    assert set(state["items"]) == {"exactly_at_horizon"}
