"""load_config の既定値補完。"""

import pytest
import yaml

from daily_feeds.config import load_config


def write_config(tmp_path, data):
    p = tmp_path / "feeds.yml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def test_settings_get_defaults_when_absent(tmp_path):
    cfg = load_config(write_config(tmp_path, {"feeds": []}))
    s = cfg["settings"]
    assert s["retention_days"] == 14
    assert s["max_items"] == 100
    assert s["score_batch_size"] == 20
    assert s["gemini_thinking_level"] is None
    assert s["feed_title"] == "daily-feeds"


def test_json_max_items_defaults_for_configs_written_before_it_existed(tmp_path):
    """json_max_items を知らない頃の feeds.yml でも読めること。"""
    cfg = load_config(write_config(tmp_path, {"feeds": [], "settings": {"max_items": 50}}))
    assert cfg["settings"]["json_max_items"] == 400
    assert cfg["settings"]["max_items"] == 50


def test_explicit_values_are_kept(tmp_path):
    cfg = load_config(
        write_config(
            tmp_path,
            {"settings": {"retention_days": 3, "json_max_items": 10, "gemini_model": "x"}},
        )
    )
    s = cfg["settings"]
    assert s["retention_days"] == 3
    assert s["json_max_items"] == 10
    assert s["gemini_model"] == "x"


def test_user_agent_is_derived_from_site_url(tmp_path):
    cfg = load_config(write_config(tmp_path, {"settings": {"site_url": "https://example.com"}}))
    assert cfg["settings"]["user_agent"] == "daily-feeds/1.0 (+https://example.com)"


def test_user_agent_is_not_overwritten(tmp_path):
    cfg = load_config(write_config(tmp_path, {"settings": {"user_agent": "custom/1.0"}}))
    assert cfg["settings"]["user_agent"] == "custom/1.0"


def test_missing_settings_block_is_created(tmp_path):
    cfg = load_config(write_config(tmp_path, {"feeds": [{"url": "https://example.com/f"}]}))
    assert "settings" in cfg
    assert cfg["feeds"] == [{"url": "https://example.com/f"}]


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yml")
