"""重複排除・スコアのマージ・並び順・件数上限と、フィクスチャを使った通し実行。"""

import json
from xml.etree import ElementTree

import pytest
import yaml

from daily_feeds import cli
from daily_feeds.cli import main, parse_args

# ---------------------------------------------------------------- parse_args


def test_parse_args_defaults_are_repository_relative():
    args = parse_args([])
    assert str(args.config) == "feeds.yml"
    assert str(args.state) == "data/state.json"
    assert str(args.docs_dir) == "docs"
    assert args.dry_run is False
    assert args.no_gemini is False


def test_parse_args_accepts_path_overrides():
    args = parse_args(["--config", "/x/f.yml", "--state", "/x/s.json", "--docs-dir", "/x/docs"])
    assert str(args.config) == "/x/f.yml"
    assert str(args.state) == "/x/s.json"
    assert str(args.docs_dir) == "/x/docs"


def test_parse_args_flags():
    args = parse_args(["--dry-run", "--no-gemini"])
    assert args.dry_run is True
    assert args.no_gemini is True


# ---------------------------------------------------------------- 通し実行


def write_workspace(tmp_path, feeds, **settings):
    cfg = {
        "feeds": feeds,
        "settings": {
            "retention_days": 100000,  # フィクスチャの日付が保持期間から落ちないように
            "site_url": "https://example.com/feeds",
            **settings,
        },
    }
    config = tmp_path / "feeds.yml"
    config.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return {
        "config": str(config),
        "state": str(tmp_path / "data" / "state.json"),
        "docs": str(tmp_path / "docs"),
    }


def run(ws, *extra):
    main(
        [
            "--no-gemini",
            "--config",
            ws["config"],
            "--state",
            ws["state"],
            "--docs-dir",
            ws["docs"],
            *extra,
        ]
    )


def test_end_to_end_over_rss_and_atom_fixtures(tmp_path, fixture_path):
    ws = write_workspace(
        tmp_path,
        [
            {"name": "RSS", "url": str(fixture_path / "rss20.xml"), "category": "tech"},
            {"name": "Atom", "url": str(fixture_path / "atom.xml"), "category": "ai"},
        ],
    )
    run(ws)

    payload = json.loads((tmp_path / "docs" / "feed.json").read_text(encoding="utf-8"))
    titles = [i["title"] for i in payload["items"]]
    assert "最初の記事" in titles
    assert "Atomの記事" in titles

    channel = ElementTree.parse(tmp_path / "docs" / "feed.xml").getroot().find("channel")
    assert len(channel.findall("item")) == len(payload["items"])
    assert (tmp_path / "docs" / "feed-ranked.xml").exists()


def test_atom_content_is_used_when_the_summary_is_thin(tmp_path, fixture_path):
    ws = write_workspace(tmp_path, [{"name": "Atom", "url": str(fixture_path / "atom.xml")}])
    run(ws)
    payload = json.loads((tmp_path / "docs" / "feed.json").read_text(encoding="utf-8"))
    assert "本文はこちらにあり" in payload["items"][0]["summary"]


def test_unscored_items_get_the_fallback_score(tmp_path, fixture_path):
    ws = write_workspace(tmp_path, [{"name": "RSS", "url": str(fixture_path / "rss20.xml")}])
    run(ws)
    payload = json.loads((tmp_path / "docs" / "feed.json").read_text(encoding="utf-8"))
    assert {i["score"] for i in payload["items"]} == {50}
    assert {i["reason"] for i in payload["items"]} == {""}


def test_cached_scores_are_merged_in(tmp_path, fixture_path):
    from daily_feeds.fetching import item_id

    ws = write_workspace(tmp_path, [{"name": "RSS", "url": str(fixture_path / "rss20.xml")}])
    known = item_id("https://example.com/posts/2")
    state_path = tmp_path / "data" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"items": {known: {"score": 91, "reason": "既出", "first_seen": "2026-01-15"}}}),
        encoding="utf-8",
    )
    run(ws)
    payload = json.loads((tmp_path / "docs" / "feed.json").read_text(encoding="utf-8"))
    scored = {i["id"]: i for i in payload["items"]}
    assert scored[known]["score"] == 91
    assert scored[known]["reason"] == "既出"


def test_duplicate_links_across_feeds_are_collapsed(tmp_path, fixture_path):
    """同じ記事が2フィードに出ても1件にする(追跡パラメータ違いも同一視)。"""
    ws = write_workspace(
        tmp_path,
        [
            {"name": "One", "url": str(fixture_path / "rss20.xml")},
            {"name": "Two", "url": str(fixture_path / "rss20.xml")},
        ],
    )
    run(ws)
    payload = json.loads((tmp_path / "docs" / "feed.json").read_text(encoding="utf-8"))
    ids = [i["id"] for i in payload["items"]]
    assert len(ids) == len(set(ids)) == 2
    assert all(i["source"] == "One" for i in payload["items"])  # 最初に見つけた側を残す


def test_broken_feed_is_reported_without_stopping_the_run(tmp_path, fixture_path):
    ws = write_workspace(
        tmp_path,
        [
            {"name": "Broken", "url": str(fixture_path / "broken.xml")},
            {"name": "RSS", "url": str(fixture_path / "rss20.xml")},
        ],
    )
    run(ws)
    payload = json.loads((tmp_path / "docs" / "feed.json").read_text(encoding="utf-8"))
    assert [i["source"] for i in payload["items"]] == ["RSS", "RSS"]


def test_run_fails_when_nothing_could_be_fetched(tmp_path, fixture_path):
    """全滅したら前回の出力を残したいので異常終了する。"""
    ws = write_workspace(tmp_path, [{"name": "Missing", "url": str(tmp_path / "nope.xml")}])
    with pytest.raises(SystemExit) as exc:
        run(ws)
    assert exc.value.code == 1
    assert not (tmp_path / "docs").exists()


def test_items_outside_the_retention_window_are_dropped(tmp_path, fixture_path):
    ws = write_workspace(
        tmp_path,
        [{"name": "RSS", "url": str(fixture_path / "rss20.xml")}],
        retention_days=1,
    )
    run(ws)
    payload = json.loads((tmp_path / "docs" / "feed.json").read_text(encoding="utf-8"))
    assert payload["items"] == []


def test_rss_respects_max_items_while_json_uses_its_own_limit(tmp_path, fixture_path):
    ws = write_workspace(
        tmp_path,
        [{"name": "RSS", "url": str(fixture_path / "rss20.xml")}],
        max_items=1,
        json_max_items=400,
    )
    run(ws)
    channel = ElementTree.parse(tmp_path / "docs" / "feed.xml").getroot().find("channel")
    payload = json.loads((tmp_path / "docs" / "feed.json").read_text(encoding="utf-8"))
    assert len(channel.findall("item")) == 1
    assert len(payload["items"]) == 2


def test_feeds_are_sorted_newest_first(tmp_path, fixture_path):
    ws = write_workspace(tmp_path, [{"name": "RSS", "url": str(fixture_path / "rss20.xml")}])
    run(ws)
    payload = json.loads((tmp_path / "docs" / "feed.json").read_text(encoding="utf-8"))
    published = [i["published"] for i in payload["items"]]
    assert published == sorted(published, reverse=True)


def test_ranked_feed_is_sorted_by_score(tmp_path, fixture_path):
    from daily_feeds.fetching import item_id

    ws = write_workspace(tmp_path, [{"name": "RSS", "url": str(fixture_path / "rss20.xml")}])
    older = item_id("https://example.com/posts/1")  # 日付は古いがスコアは高い
    state_path = tmp_path / "data" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"items": {older: {"score": 99}}}), encoding="utf-8")
    run(ws)
    channel = ElementTree.parse(tmp_path / "docs" / "feed-ranked.xml").getroot().find("channel")
    assert channel.findall("item")[0].find("title").text.startswith("[99]")


def test_dry_run_writes_nothing(tmp_path, fixture_path):
    ws = write_workspace(tmp_path, [{"name": "RSS", "url": str(fixture_path / "rss20.xml")}])
    run(ws, "--dry-run")
    assert not (tmp_path / "docs").exists()
    assert not (tmp_path / "data" / "state.json").exists()


def test_state_is_pruned_and_saved(tmp_path, fixture_path):
    # 掃除の地平線は cutoff からさらに30日前。保持期間を既定に戻して現実的な位置に置く
    ws = write_workspace(
        tmp_path,
        [{"name": "RSS", "url": str(fixture_path / "rss20.xml")}],
        retention_days=14,
    )
    state_path = tmp_path / "data" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"items": {"ancient": {"first_seen": "1990-01-01T00:00:00+00:00"}}}),
        encoding="utf-8",
    )
    run(ws)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert "ancient" not in saved["items"]


def test_gemini_is_not_called_with_no_gemini(tmp_path, fixture_path, monkeypatch):
    """--no-gemini でクライアント生成にすら到達しないこと。"""
    from daily_feeds import scoring

    monkeypatch.setattr(
        scoring, "make_client", lambda: pytest.fail("--no-gemini なのに Gemini を呼んだ")
    )
    ws = write_workspace(tmp_path, [{"name": "RSS", "url": str(fixture_path / "rss20.xml")}])
    run(ws)


def test_module_entry_point_is_wired():
    assert cli.main is main
