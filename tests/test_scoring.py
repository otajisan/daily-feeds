"""Geminiスコアリングのリトライ判定・応答解析・キャッシュ方針。

score_new_items の「失敗をキャッシュしない」は過去に実際壊れていた箇所なので、
フェイククライアントで明示的に守っている。
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from daily_feeds import scoring
from daily_feeds.scoring import (
    DEFAULT_SCORE,
    TransientScoringError,
    finish_reason,
    gemini_score_batch,
    is_retryable,
    make_client,
    parse_score_response,
    score_new_items,
)

CUTOFF = datetime(2026, 1, 15, tzinfo=UTC)
CFG = {"settings": {"gemini_model": "test-model", "score_batch_size": 2}}


def coded(code, message="boom"):
    """google-genai の例外のように .code を持つ例外。"""
    e = RuntimeError(message)
    e.code = code
    return e


def named(name, message="boom"):
    """httpx などライブラリ固有のコネクション系例外を型名だけ再現する。"""
    return type(name, (Exception,), {})(message)


# ---------------------------------------------------------------- is_retryable


@pytest.mark.parametrize("code", [408, 425, 429, 500, 502, 503, 504])
def test_retryable_status_codes(code):
    assert is_retryable(coded(code)) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 422])
def test_client_errors_are_not_retried(code):
    """投げ直しても直らないリクエスト側のエラー。"""
    assert is_retryable(coded(code)) is False


def test_client_error_wins_over_a_retryable_looking_message():
    """4xx は本文に 503 などが混ざっていてもリトライしない。"""
    assert is_retryable(coded(404, "503 in the body")) is False


@pytest.mark.parametrize(
    "exc",
    [
        TransientScoringError("empty response"),
        json.JSONDecodeError("bad", "{}", 0),
        ConnectionError("reset"),
        TimeoutError("slow"),
    ],
)
def test_transient_exception_types_are_retried(exc):
    assert is_retryable(exc) is True


@pytest.mark.parametrize(
    "name",
    [
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
    ],
)
def test_connection_exceptions_are_retried_by_type_name(name):
    """httpx等の型を直接importできないので名前で判定している。"""
    assert is_retryable(named(name)) is True


@pytest.mark.parametrize(
    "message",
    [
        "429 Too Many Requests",
        "500 Internal Server Error",
        "502 Bad Gateway",
        "503 Service Unavailable",
        "504 Gateway Timeout",
        "RESOURCE_EXHAUSTED",
        "UNAVAILABLE",
        "INTERNAL",
        "DEADLINE_EXCEEDED",
        "Server disconnected without sending a response.",
        "Connection reset by peer",
        "request timed out",
    ],
)
def test_retryable_message_hints(message):
    assert is_retryable(RuntimeError(message)) is True


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("blocked response (finish_reason=SAFETY)"),
        RuntimeError("something else entirely"),
        KeyError("missing"),
    ],
)
def test_permanent_failures_are_not_retried(exc):
    assert is_retryable(exc) is False


def test_a_string_code_is_not_treated_as_a_status():
    """.code が文字列なら status 判定はせず、本文の手がかりだけで決める。"""
    e = RuntimeError("nope")
    e.code = "429"
    assert is_retryable(e) is False


# ---------------------------------------------------------------- parse_score_response

BATCH = [
    {"id": "aaa", "source": "Src", "title": "記事A", "summary": "要約A"},
    {"id": "bbb", "source": "Src", "title": "記事B", "summary": "要約B"},
]


def test_parse_score_response_normal():
    text = json.dumps([{"idx": 0, "score": 90, "reason": "重要"}])
    assert parse_score_response(text, BATCH) == {"aaa": {"score": 90, "reason": "重要"}}


def test_parse_score_response_unwraps_a_dict_response():
    """スキーマを無視して配列をオブジェクトで包んで返してくる場合がある。"""
    text = json.dumps({"results": [{"idx": 1, "score": 40, "reason": "普通"}]})
    assert parse_score_response(text, BATCH) == {"bbb": {"score": 40, "reason": "普通"}}


def test_parse_score_response_ignores_out_of_range_index():
    text = json.dumps([{"idx": 9, "score": 90, "reason": "x"}, {"idx": -1, "score": 10}])
    assert parse_score_response(text, BATCH) == {}


@pytest.mark.parametrize(("raw", "expected"), [(150, 100), (-20, 0), (0, 0), (100, 100)])
def test_parse_score_response_clamps_scores(raw, expected):
    text = json.dumps([{"idx": 0, "score": raw, "reason": ""}])
    assert parse_score_response(text, BATCH)["aaa"]["score"] == expected


@pytest.mark.parametrize(
    "row",
    [
        "not a dict",
        {"score": 50, "reason": "idxが無い"},
        {"idx": 0, "reason": "scoreが無い"},
        {"idx": "x", "score": 50},
        {"idx": 0, "score": None},
        {"idx": 0, "score": "high"},
    ],
)
def test_parse_score_response_skips_malformed_rows(row):
    assert parse_score_response(json.dumps([row]), BATCH) == {}


def test_parse_score_response_accepts_numeric_strings():
    text = json.dumps([{"idx": "0", "score": "77", "reason": "文字列"}])
    assert parse_score_response(text, BATCH)["aaa"]["score"] == 77


def test_parse_score_response_truncates_long_reasons():
    text = json.dumps([{"idx": 0, "score": 50, "reason": "あ" * 300}])
    assert len(parse_score_response(text, BATCH)["aaa"]["reason"]) == 100


def test_parse_score_response_defaults_a_missing_reason():
    text = json.dumps([{"idx": 0, "score": 50}])
    assert parse_score_response(text, BATCH)["aaa"]["reason"] == ""


def test_parse_score_response_raises_on_invalid_json():
    with pytest.raises(json.JSONDecodeError):
        parse_score_response("not json", BATCH)


# ---------------------------------------------------------------- finish_reason


def test_finish_reason_reports_a_prompt_level_block():
    resp = SimpleNamespace(prompt_feedback=SimpleNamespace(block_reason="SAFETY"))
    assert finish_reason(resp) == "BLOCKED(SAFETY)"


def test_finish_reason_reads_the_first_candidate():
    resp = SimpleNamespace(
        prompt_feedback=None, candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")]
    )
    assert finish_reason(resp) == "MAX_TOKENS"


def test_finish_reason_without_candidates():
    assert finish_reason(SimpleNamespace(prompt_feedback=None, candidates=[])) == "NO_CANDIDATES"


def test_finish_reason_on_an_unexpected_shape():
    assert finish_reason(object()) == "NO_CANDIDATES"


# ---------------------------------------------------------------- gemini_score_batch


class FakeClient:
    """client.models.generate_content の最小の代役。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        result = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


def response(text):
    return SimpleNamespace(text=text)


def empty_response(reason):
    return SimpleNamespace(
        text="", prompt_feedback=None, candidates=[SimpleNamespace(finish_reason=reason)]
    )


def test_gemini_score_batch_returns_parsed_scores():
    client = FakeClient([response(json.dumps([{"idx": 0, "score": 88, "reason": "重要"}]))])
    assert gemini_score_batch(client, CFG, BATCH) == {"aaa": {"score": 88, "reason": "重要"}}
    assert client.calls[0]["model"] == "test-model"


def test_gemini_score_batch_retries_a_transient_failure():
    client = FakeClient([coded(503), response(json.dumps([{"idx": 0, "score": 10, "reason": ""}]))])
    assert gemini_score_batch(client, CFG, BATCH)["aaa"]["score"] == 10
    assert len(client.calls) == 2


def test_gemini_score_batch_retries_an_empty_response():
    client = FakeClient(
        [empty_response("STOP"), response(json.dumps([{"idx": 0, "score": 5, "reason": ""}]))]
    )
    assert gemini_score_batch(client, CFG, BATCH)["aaa"]["score"] == 5
    assert len(client.calls) == 2


@pytest.mark.parametrize("reason", ["SAFETY", "MAX_TOKENS", "RECITATION", "PROHIBITED_CONTENT"])
def test_gemini_score_batch_does_not_retry_terminal_finish_reasons(reason):
    """入力が同じ限り結果が変わらない終了理由は即あきらめる。"""
    client = FakeClient([empty_response(reason)])
    with pytest.raises(ValueError, match="blocked response"):
        gemini_score_batch(client, CFG, BATCH)
    assert len(client.calls) == 1


def test_gemini_score_batch_stops_after_the_retry_budget():
    client = FakeClient([coded(503)])
    with pytest.raises(RuntimeError):
        gemini_score_batch(client, CFG, BATCH)
    assert len(client.calls) == scoring.SCORE_RETRIES


def test_gemini_score_batch_does_not_retry_a_client_error():
    client = FakeClient([coded(400)])
    with pytest.raises(RuntimeError):
        gemini_score_batch(client, CFG, BATCH)
    assert len(client.calls) == 1


def test_gemini_score_batch_omits_thinking_config_by_default():
    client = FakeClient([response("[]")])
    gemini_score_batch(client, CFG, BATCH)
    assert "thinking_config" not in client.calls[0]["config"]


def test_gemini_score_batch_passes_thinking_level_when_configured():
    cfg = {"settings": {**CFG["settings"], "gemini_thinking_level": "low"}}
    client = FakeClient([response("[]")])
    gemini_score_batch(client, cfg, BATCH)
    assert client.calls[0]["config"]["thinking_config"] == {"thinking_level": "low"}


def test_gemini_score_batch_includes_titles_in_the_prompt():
    batch = [{"id": "a", "source": "Src", "title": "タイトル", "summary": "要約"}]
    client = FakeClient([response("[]")])
    gemini_score_batch(client, CFG, batch)
    assert "タイトル" in client.calls[0]["contents"]
    assert "[Src]" in client.calls[0]["contents"]


# ---------------------------------------------------------------- score_new_items


def item(item_id_, published="2026-01-20T00:00:00+00:00", **extra):
    return {
        "id": item_id_,
        "published": published,
        "title": f"title-{item_id_}",
        "summary": "summary",
        "source": "Src",
        **extra,
    }


def use_fake_client(monkeypatch, client):
    monkeypatch.setattr(scoring, "make_client", lambda: client)
    return client


def test_score_new_items_does_not_cache_failures(monkeypatch):
    """スコアリングに失敗した記事はキャッシュせず、次回実行で再挑戦させる。"""
    client = use_fake_client(monkeypatch, FakeClient([coded(503)]))
    state = {"items": {}}
    score_new_items([item("a"), item("b")], state, CFG, CUTOFF, use_gemini=True)
    assert state["items"] == {}
    assert len(client.calls) == scoring.SCORE_RETRIES


def test_score_new_items_caches_only_what_came_back(monkeypatch):
    """一部だけ返ってきた場合、欠けた記事はキャッシュしない。"""
    use_fake_client(
        monkeypatch, FakeClient([response(json.dumps([{"idx": 0, "score": 70, "reason": "r"}]))])
    )
    state = {"items": {}}
    score_new_items([item("a"), item("b")], state, CFG, CUTOFF, use_gemini=True)
    assert set(state["items"]) == {"a"}
    assert state["items"]["a"] == {"score": 70, "reason": "r", "first_seen": item("a")["published"]}


def test_score_new_items_skips_already_scored_items(monkeypatch):
    client = use_fake_client(monkeypatch, FakeClient([response("[]")]))
    state = {"items": {"a": {"score": 60, "reason": "既出"}}}
    score_new_items([item("a")], state, CFG, CUTOFF, use_gemini=True)
    assert client.calls == []
    assert state["items"]["a"]["score"] == 60


def test_score_new_items_rescores_entries_missing_a_score(monkeypatch):
    """first_seen だけあってスコアが無いエントリは対象に含める。"""
    client = use_fake_client(
        monkeypatch, FakeClient([response(json.dumps([{"idx": 0, "score": 33, "reason": "r"}]))])
    )
    state = {"items": {"a": {"first_seen": "2026-01-18T00:00:00+00:00"}}}
    score_new_items([item("a")], state, CFG, CUTOFF, use_gemini=True)
    assert len(client.calls) == 1
    assert state["items"]["a"]["score"] == 33


def test_score_new_items_preserves_the_original_first_seen(monkeypatch):
    use_fake_client(
        monkeypatch, FakeClient([response(json.dumps([{"idx": 0, "score": 33, "reason": "r"}]))])
    )
    state = {"items": {"a": {"first_seen": "2025-12-01T00:00:00+00:00"}}}
    score_new_items([item("a")], state, CFG, CUTOFF, use_gemini=True)
    assert state["items"]["a"]["first_seen"] == "2025-12-01T00:00:00+00:00"


def test_score_new_items_ignores_items_outside_the_retention_window(monkeypatch):
    """保持期間外の記事はどうせ載らないのでスコアリングしない(APIコスト削減)。"""
    client = use_fake_client(monkeypatch, FakeClient([response("[]")]))
    state = {"items": {}}
    score_new_items([item("old", published="2026-01-01T00:00:00+00:00")], state, CFG, CUTOFF, True)
    assert client.calls == []
    assert state["items"] == {}


def test_score_new_items_falls_back_without_gemini(monkeypatch):
    called = []
    monkeypatch.setattr(scoring, "make_client", lambda: called.append(1))
    state = {"items": {}}
    score_new_items([item("a")], state, CFG, CUTOFF, use_gemini=False)
    assert called == []
    assert state["items"] == {}


def test_score_new_items_falls_back_when_the_client_cannot_be_built(monkeypatch):
    """APIキーが無い場合。集約自体は続行させる。"""
    monkeypatch.setattr(scoring, "make_client", lambda: None)
    state = {"items": {}}
    score_new_items([item("a")], state, CFG, CUTOFF, use_gemini=True)
    assert state["items"] == {}


def test_score_new_items_splits_into_batches(monkeypatch):
    client = use_fake_client(monkeypatch, FakeClient([response("[]")]))
    state = {"items": {}}
    score_new_items([item(f"i{n}") for n in range(5)], state, CFG, CUTOFF, use_gemini=True)
    assert len(client.calls) == 3  # score_batch_size=2 → 2 + 2 + 1


def test_score_new_items_survives_one_failing_batch(monkeypatch):
    ok = response(json.dumps([{"idx": 0, "score": 42, "reason": "r"}]))
    use_fake_client(monkeypatch, FakeClient([ok, ValueError("permanent"), ok]))
    state = {"items": {}}
    score_new_items([item(f"i{n}") for n in range(4)], state, CFG, CUTOFF, use_gemini=True)
    assert "i0" in state["items"]  # 1バッチ目は残る
    assert "i2" not in state["items"]  # 2バッチ目の失敗はキャッシュされない


# ---------------------------------------------------------------- make_client


def test_make_client_returns_none_without_an_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert make_client() is None


def test_make_client_builds_a_client_with_the_api_key(monkeypatch):
    import sys
    import types

    built = {}
    fake_google = types.ModuleType("google")
    fake_google.genai = types.SimpleNamespace(
        Client=lambda api_key: built.setdefault("api_key", api_key)
    )
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    assert make_client() == "secret"
    assert built["api_key"] == "secret"


def test_make_client_returns_none_when_the_sdk_fails(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    monkeypatch.setitem(__import__("sys").modules, "google.genai", None)
    assert make_client() is None


def test_default_score_is_the_neutral_midpoint():
    assert DEFAULT_SCORE == 50
