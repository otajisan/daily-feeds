"""テスト共通の前提。

いちばん重要なのはネットワーク遮断。うっかり実フィードやGeminiを叩くテストを書くと、
CIが外部サービスの機嫌で落ちるようになり、テストが信用できなくなる。
"""

import socket
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


class NetworkAccessDenied(RuntimeError):
    """テスト中にネットワークへ出ようとした。"""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise NetworkAccessDenied("テスト中のネットワークアクセスは禁止されている")

    monkeypatch.setattr(socket, "socket", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """リトライのバックオフでテストを待たせない。"""
    monkeypatch.setattr("time.sleep", lambda *_: None)


@pytest.fixture
def fixture_path():
    return FIXTURES
