"""フィード取得とエントリ正規化。

entry_datetime のUTC解釈は過去に実際壊れていた(time.mktime を使っていて
JSTで9時間ずれた)ので、タイムゾーンを固定した回帰テストを置いている。
"""

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from daily_feeds import fetching
from daily_feeds.fetching import (
    entry_datetime,
    entry_summary,
    fetch_all,
    fetch_feed,
    item_id,
    normalize_url,
    strip_html,
)

NOW = datetime(2026, 1, 20, 0, 0, tzinfo=UTC)


@pytest.fixture
def tokyo_timezone(monkeypatch):
    """ローカルタイムゾーンをJSTに固定する。

    UTC実行のCIでも「UTCとして解釈しているか」を検出できるようにするため。
    time.mktime を使った実装ならこの状態で9時間ずれる。
    """
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


def struct(y, mo, d, h, mi, s=0):
    return time.struct_time((y, mo, d, h, mi, s, 0, 1, 0))


# ---------------------------------------------------------------- entry_datetime


def test_entry_datetime_interprets_struct_time_as_utc(tokyo_timezone):
    """*_parsed はUTCのstruct_time。ローカル時刻として読むと9時間ずれる。"""
    entry = SimpleNamespace(published_parsed=struct(2026, 1, 15, 12, 0))
    assert entry_datetime(entry, NOW) == datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def test_entry_datetime_prefers_published_over_updated():
    entry = SimpleNamespace(
        published_parsed=struct(2026, 1, 15, 1, 0),
        updated_parsed=struct(2026, 1, 16, 1, 0),
    )
    assert entry_datetime(entry, NOW) == datetime(2026, 1, 15, 1, 0, tzinfo=UTC)


def test_entry_datetime_falls_back_to_updated():
    entry = SimpleNamespace(updated_parsed=struct(2026, 1, 16, 1, 0))
    assert entry_datetime(entry, NOW) == datetime(2026, 1, 16, 1, 0, tzinfo=UTC)


def test_entry_datetime_clamps_future_dates_to_now():
    """未来日付の記事が新着順の上位に居座らないようにする。"""
    entry = SimpleNamespace(published_parsed=struct(2030, 1, 1, 0, 0))
    assert entry_datetime(entry, NOW) == NOW


def test_entry_datetime_returns_none_without_any_date():
    assert entry_datetime(SimpleNamespace(), NOW) is None


def test_entry_datetime_ignores_empty_parsed_values():
    assert entry_datetime(SimpleNamespace(published_parsed=None, updated_parsed=None), NOW) is None


# ---------------------------------------------------------------- normalize_url / item_id


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/a?utm_source=rss&id=1", "https://example.com/a?id=1"),
        ("https://example.com/a?UTM_MEDIUM=x", "https://example.com/a"),
        ("https://example.com/a?fbclid=z", "https://example.com/a"),
        ("https://example.com/a?gclid=z&ref=y&spm=w", "https://example.com/a"),
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com/", "https://example.com/"),
        ("HTTPS://Example.COM/Path", "https://example.com/Path"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com/a?b=1&c=2", "https://example.com/a?b=1&c=2"),
    ],
)
def test_normalize_url(url, expected):
    assert normalize_url(url) == expected


def test_normalize_url_returns_input_when_unparseable():
    """urlsplit が投げる入力(壊れたIPv6リテラル)でも例外を外に出さない。"""
    broken = "http://[::1"
    assert normalize_url(broken) == broken


def test_item_id_is_stable_across_tracking_parameters():
    a = item_id("https://example.com/post?utm_source=twitter")
    b = item_id("https://example.com/post/")
    assert a == b


def test_item_id_is_16_hex_chars():
    got = item_id("https://example.com/post")
    assert len(got) == 16
    assert all(c in "0123456789abcdef" for c in got)


def test_item_id_differs_for_different_urls():
    assert item_id("https://example.com/a") != item_id("https://example.com/b")


# ---------------------------------------------------------------- strip_html / entry_summary


def test_strip_html_removes_tags_and_collapses_whitespace():
    assert strip_html("<p>hello</p>\n\n  <b>world</b>") == "hello world"


def test_strip_html_unescapes_entities():
    assert strip_html("a &amp; b &lt;c&gt;") == "a & b <c>"


def test_strip_html_truncates_to_limit():
    assert strip_html("x" * 600) == "x" * 500
    assert strip_html("x" * 600, limit=10) == "x" * 10


def test_strip_html_handles_none():
    assert strip_html(None) == ""


def test_entry_summary_prefers_a_substantial_summary():
    summary = "あ" * 80
    entry = {"summary": summary, "content": [{"value": "い" * 200}]}
    assert entry_summary(entry) == summary


def test_entry_summary_falls_back_to_content_when_summary_is_thin():
    entry = {"summary": "短い", "content": [{"value": "<p>" + "い" * 200 + "</p>"}]}
    assert entry_summary(entry) == "い" * 200


def test_entry_summary_picks_the_longest_content():
    entry = {"summary": "", "content": [{"value": "short"}, {"value": "much longer body"}]}
    assert entry_summary(entry) == "much longer body"


def test_entry_summary_returns_empty_when_nothing_is_available():
    assert entry_summary({}) == ""
    assert entry_summary({"summary": "", "content": []}) == ""


# ---------------------------------------------------------------- fetch_feed


class FakeParsed(dict):
    """feedparser の FeedParserDict を最小限まねる(属性でも添字でも引ける)。"""

    def __init__(self, entries=(), status=None, bozo=False, bozo_exception=None):
        super().__init__(bozo_exception=bozo_exception)
        self.entries = list(entries)
        self.bozo = bozo
        if status is not None:
            self.status = status


@pytest.fixture
def parse_calls(monkeypatch):
    """feedparser.parse を差し替え、呼ばれた回数を数える。"""
    calls = []

    def install(results):
        def fake_parse(url, agent=None):
            calls.append(url)
            result = results[min(len(calls) - 1, len(results) - 1)]
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(fetching.feedparser, "parse", fake_parse)
        return calls

    return install


def test_fetch_feed_returns_entries_without_retrying(parse_calls):
    calls = parse_calls([FakeParsed(entries=[{"link": "x"}], status=200)])
    parsed, note = fetch_feed("https://example.com/feed", "agent/1.0")
    assert note == ""
    assert parsed.entries == [{"link": "x"}]
    assert len(calls) == 1


def test_fetch_feed_accepts_a_clean_but_empty_feed(parse_calls):
    calls = parse_calls([FakeParsed(entries=[], status=200, bozo=False)])
    parsed, note = fetch_feed("https://example.com/feed", "agent/1.0")
    assert note == "0 entries"
    assert parsed is not None
    assert len(calls) == 1


def test_fetch_feed_gives_up_immediately_on_404(parse_calls):
    """投げ直しても直らない4xxでリトライしない。"""
    calls = parse_calls([FakeParsed(entries=[], status=404)])
    parsed, note = fetch_feed("https://example.com/feed", "agent/1.0")
    assert (parsed, note) == (None, "HTTP 404")
    assert len(calls) == 1


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_fetch_feed_retries_transient_statuses(parse_calls, status):
    calls = parse_calls([FakeParsed(entries=[], status=status)])
    parsed, note = fetch_feed("https://example.com/feed", "agent/1.0")
    assert (parsed, note) == (None, f"HTTP {status}")
    assert len(calls) == fetching.FETCH_RETRIES + 1


def test_fetch_feed_retries_a_304_response(parse_calls):
    calls = parse_calls([FakeParsed(entries=[], status=304)])
    _, note = fetch_feed("https://example.com/feed", "agent/1.0")
    assert note == "HTTP 304"
    assert len(calls) == fetching.FETCH_RETRIES + 1


def test_fetch_feed_recovers_when_a_retry_succeeds(parse_calls):
    calls = parse_calls(
        [FakeParsed(entries=[], status=503), FakeParsed(entries=[{"link": "x"}], status=200)]
    )
    parsed, note = fetch_feed("https://example.com/feed", "agent/1.0")
    assert note == ""
    assert parsed.entries == [{"link": "x"}]
    assert len(calls) == 2


def test_fetch_feed_reports_exceptions_by_type_and_message(parse_calls):
    calls = parse_calls([OSError("connection reset")])
    parsed, note = fetch_feed("https://example.com/feed", "agent/1.0")
    assert parsed is None
    assert note == "OSError: connection reset"
    assert len(calls) == fetching.FETCH_RETRIES + 1


def test_fetch_feed_reports_the_parse_error_for_broken_xml(parse_calls):
    parse_calls([FakeParsed(entries=[], bozo=True, bozo_exception=ValueError("mismatched tag"))])
    parsed, note = fetch_feed("https://example.com/feed", "agent/1.0")
    assert parsed is None
    assert "mismatched tag" in note


def test_fetch_feed_falls_back_to_a_generic_note(parse_calls):
    parse_calls([FakeParsed(entries=[], bozo=True)])
    parsed, note = fetch_feed("https://example.com/feed", "agent/1.0")
    assert (parsed, note) == (None, "no entries")


def test_fetch_feed_passes_the_user_agent(monkeypatch):
    seen = {}

    def fake_parse(url, agent=None):
        seen["agent"] = agent
        return FakeParsed(entries=[{"link": "x"}], status=200)

    monkeypatch.setattr(fetching.feedparser, "parse", fake_parse)
    fetch_feed("https://example.com/feed", "daily-feeds/1.0")
    assert seen["agent"] == "daily-feeds/1.0"


# ---------------------------------------------------------------- fetch_all

CFG = {
    "settings": {"user_agent": "agent/1.0"},
    "feeds": [
        {"name": "A", "url": "https://a.example/feed", "category": "tech"},
        {"name": "B", "url": "https://b.example/feed"},
    ],
}


def test_fetch_all_normalizes_entries_into_items(monkeypatch):
    entry = {
        "link": "https://a.example/post?utm_source=rss",
        "title": "  タイトル  ",
        "summary": "要約",
    }
    monkeypatch.setattr(
        fetching, "fetch_feed", lambda url, agent: (FakeParsed(entries=[entry], status=200), "")
    )
    items, failures = fetch_all(CFG, NOW)
    assert failures == []
    assert len(items) == 2  # フィード2本ぶん
    first = items[0]
    assert first["title"] == "タイトル"
    assert first["source"] == "A"
    assert first["category"] == "tech"
    assert first["id"] == item_id(entry["link"])
    assert first["published"] == NOW.isoformat()  # 日時が無いエントリは now を使う


def test_fetch_all_defaults_category_to_empty_string(monkeypatch):
    entry = {"link": "https://b.example/post"}
    monkeypatch.setattr(
        fetching, "fetch_feed", lambda url, agent: (FakeParsed(entries=[entry], status=200), "")
    )
    items, _ = fetch_all(CFG, NOW)
    assert items[1]["category"] == ""
    assert items[1]["title"] == "(no title)"


def test_fetch_all_skips_entries_without_a_link(monkeypatch):
    monkeypatch.setattr(
        fetching,
        "fetch_feed",
        lambda url, agent: (FakeParsed(entries=[{"title": "リンクなし"}], status=200), ""),
    )
    items, failures = fetch_all(CFG, NOW)
    assert items == []
    assert failures == []


def test_fetch_all_records_failures_by_feed_name(monkeypatch):
    def fetch(url, agent):
        if "a.example" in url:
            return None, "HTTP 404"
        return FakeParsed(entries=[{"link": "https://b.example/p"}], status=200), ""

    monkeypatch.setattr(fetching, "fetch_feed", fetch)
    items, failures = fetch_all(CFG, NOW)
    assert failures == ["A"]
    assert len(items) == 1


def test_fetch_all_uses_the_published_date_when_present(monkeypatch):
    entry = SimpleNamespace(published_parsed=struct(2026, 1, 15, 12, 0))
    entry.get = lambda k, d=None: {"link": "https://a.example/p"}.get(k, d)
    monkeypatch.setattr(
        fetching, "fetch_feed", lambda url, agent: (FakeParsed(entries=[entry], status=200), "")
    )
    items, _ = fetch_all(CFG, NOW)
    assert items[0]["published"] == datetime(2026, 1, 15, 12, 0, tzinfo=UTC).isoformat()


def test_fetch_all_returns_nothing_for_an_empty_feed_list(monkeypatch):
    items, failures = fetch_all({"settings": {"user_agent": "a"}}, NOW)
    assert (items, failures) == ([], [])


def test_no_network_fixture_blocks_real_requests():
    """遮断fixtureが実際に効いていることの確認。"""
    import socket as real_socket

    with pytest.raises(Exception, match="ネットワークアクセス"):
        real_socket.create_connection(("example.com", 80))


def test_retention_window_uses_timezone_aware_datetimes():
    assert (NOW - timedelta(days=14)).tzinfo is UTC
