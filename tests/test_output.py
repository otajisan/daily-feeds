"""生成物の中身。実際にXML/JSONとしてパースし直して検証する。"""

import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import pytest

from daily_feeds.output import build_json, build_rss, rfc822, xml_text

CFG = {
    "settings": {
        "feed_title": "daily-feeds",
        "feed_description": "テスト用フィード",
        "site_url": "https://example.com/feeds",
    }
}


def item(n, score=50, reason="理由"):
    return {
        "id": f"id{n}",
        "title": f"記事{n}",
        "link": f"https://example.com/posts/{n}",
        "summary": f"要約{n}",
        "published": f"2026-01-{n:02d}T00:00:00+00:00",
        "source": "Src",
        "category": "tech",
        "score": score,
        "reason": reason,
    }


# ---------------------------------------------------------------- xml_text


def test_xml_text_strips_control_characters():
    assert xml_text("a\x00b\x08c\x1fd") == "abcd"


def test_xml_text_keeps_allowed_whitespace():
    assert xml_text("a\tb\nc\rd") == "a\tb\nc\rd"


def test_xml_text_preserves_japanese_and_emoji():
    assert xml_text("日本語とAI 🚀 絵文字") == "日本語とAI 🚀 絵文字"


def test_xml_text_escapes_markup():
    assert xml_text("<b>a & b</b>") == "&lt;b&gt;a &amp; b&lt;/b&gt;"


def test_xml_text_handles_none():
    assert xml_text(None) == ""


def test_xml_text_output_is_parseable():
    """制御文字を含む文字列でも、埋め込んだXMLがパースできること。"""
    hostile = "見出し\x0b\x0c<script>alert('x')</script>"
    doc = f"<root>{xml_text(hostile)}</root>"
    assert ElementTree.fromstring(doc).text == "見出し<script>alert('x')</script>"


# ---------------------------------------------------------------- rfc822


def test_rfc822_converts_an_iso_timestamp():
    assert parsedate_to_datetime(rfc822("2026-01-15T12:00:00+00:00")) == datetime(
        2026, 1, 15, 12, 0, tzinfo=UTC
    )


@pytest.mark.parametrize("bad", ["", "not a date", "2026-13-45"])
def test_rfc822_falls_back_to_now_for_unparseable_input(bad):
    """壊れた日時でフィード生成ごと落とさない。"""
    assert parsedate_to_datetime(rfc822(bad)).tzinfo is not None


# ---------------------------------------------------------------- build_rss


def parse_rss(path):
    root = ElementTree.parse(path).getroot()
    return root.find("channel")


def test_build_rss_writes_every_item(tmp_path):
    path = tmp_path / "feed.xml"
    build_rss([item(1), item(2), item(3)], CFG, path, "", show_score=False)
    assert len(parse_rss(path).findall("item")) == 3


def test_build_rss_preserves_order(tmp_path):
    path = tmp_path / "feed.xml"
    build_rss([item(3), item(1), item(2)], CFG, path, "", show_score=False)
    titles = [e.find("title").text for e in parse_rss(path).findall("item")]
    assert titles == ["記事3", "記事1", "記事2"]


def test_build_rss_omits_the_score_prefix_by_default(tmp_path):
    path = tmp_path / "feed.xml"
    build_rss([item(1, score=95)], CFG, path, "", show_score=False)
    assert parse_rss(path).find("item/title").text == "記事1"


def test_build_rss_prefixes_the_score_when_asked(tmp_path):
    path = tmp_path / "feed-ranked.xml"
    build_rss([item(1, score=95)], CFG, path, " (Ranked)", show_score=True)
    channel = parse_rss(path)
    assert channel.find("item/title").text == "[95] 記事1"
    assert channel.find("title").text == "daily-feeds (Ranked)"


def test_build_rss_describes_source_and_score(tmp_path):
    path = tmp_path / "feed.xml"
    build_rss([item(1, score=80, reason="重要な更新")], CFG, path, "", show_score=False)
    desc = parse_rss(path).find("item/description").text
    assert "要約1" in desc
    assert "Src" in desc
    assert "score: 80" in desc
    assert "重要な更新" in desc


def test_build_rss_self_link_points_at_the_written_file(tmp_path):
    path = tmp_path / "feed-ranked.xml"
    build_rss([item(1)], CFG, path, "", show_score=False)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    link = parse_rss(path).find("atom:link", ns)
    assert link.get("href") == "https://example.com/feeds/feed-ranked.xml"


def test_build_rss_uses_the_item_publication_date(tmp_path):
    path = tmp_path / "feed.xml"
    build_rss([item(15)], CFG, path, "", show_score=False)
    pub = parse_rss(path).find("item/pubDate").text
    assert parsedate_to_datetime(pub) == datetime(2026, 1, 15, tzinfo=UTC)


def test_build_rss_guid_is_a_permalink(tmp_path):
    path = tmp_path / "feed.xml"
    build_rss([item(1)], CFG, path, "", show_score=False)
    guid = parse_rss(path).find("item/guid")
    assert guid.get("isPermaLink") == "true"
    assert guid.text == "https://example.com/posts/1"


def test_build_rss_escapes_hostile_titles(tmp_path):
    path = tmp_path / "feed.xml"
    nasty = item(1)
    nasty["title"] = "<script>alert(1)</script> & 続き"
    build_rss([nasty], CFG, path, "", show_score=False)
    assert parse_rss(path).find("item/title").text == "<script>alert(1)</script> & 続き"


def test_build_rss_writes_an_empty_but_valid_feed(tmp_path):
    path = tmp_path / "feed.xml"
    build_rss([], CFG, path, "", show_score=False)
    assert parse_rss(path).findall("item") == []


def test_build_rss_sets_a_last_build_date(tmp_path):
    path = tmp_path / "feed.xml"
    build_rss([item(1)], CFG, path, "", show_score=False)
    assert parsedate_to_datetime(parse_rss(path).find("lastBuildDate").text).tzinfo is not None


# ---------------------------------------------------------------- build_json


def test_build_json_round_trips(tmp_path):
    path = tmp_path / "feed.json"
    items = [item(1), item(2)]
    build_json(items, CFG, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["title"] == "daily-feeds"
    assert payload["items"] == items


def test_build_json_records_a_generation_timestamp(tmp_path):
    path = tmp_path / "feed.json"
    build_json([], CFG, path)
    generated = json.loads(path.read_text(encoding="utf-8"))["generated_at"]
    assert datetime.fromisoformat(generated).tzinfo is not None


def test_build_json_is_written_compactly(tmp_path):
    """2時間ごとにコミットされるので、余計な空白は入れない。"""
    path = tmp_path / "feed.json"
    build_json([item(1)], CFG, path)
    text = path.read_text(encoding="utf-8")
    assert ", " not in text
    assert "\n" not in text


def test_build_json_keeps_japanese_readable(tmp_path):
    path = tmp_path / "feed.json"
    build_json([item(1)], CFG, path)
    assert "記事1" in path.read_text(encoding="utf-8")


def test_build_json_preserves_order(tmp_path):
    path = tmp_path / "feed.json"
    build_json([item(3), item(1)], CFG, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [i["title"] for i in payload["items"]] == ["記事3", "記事1"]
