"""WS 会话回收 / 日志降噪回归测试。

背景（2026-10-26 服务器 C 探针实测）：
- 平台会话上限约 35 秒，连接建立后 36.3±0.2 秒必被服务端丢弃；
- 每次重连服务端回放 /s/vulcan 200 条 bizType=370（xbroadcast）包，不含聊天消息；
- 心跳/ackDiff 的裸 ACK（无 lwp、无 body、code=200）曾被记为"未处理的消息类型"。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import ws_protocol
from app.services.ws_client import (
    RECONNECT_DELAY,
    XianyuWebSocketClient,
)


def _client() -> XianyuWebSocketClient:
    return XianyuWebSocketClient(1, "cookie", "token", "unb")


def test_parse_sync_package_skips_xbroadcast_items(monkeypatch):
    """bizType=370 条目必须跳过，且不做解密（每次重连 200 条）。"""
    called = {"n": 0}

    def _boom(_data):
        called["n"] += 1
        raise AssertionError("xbroadcast 条目不应进入解密")

    monkeypatch.setattr(ws_protocol, "decrypt_payload", _boom)

    package = {
        "lwp": "/s/vulcan",
        "body": {
            "syncPushPackage": {
                "data": [
                    {"bizType": 370, "objectType": 1, "streamId": "a", "data": "eyJhIjoxfQ=="},
                    {"bizType": 370, "objectType": 1, "streamId": "b", "data": "eyJiIjoyfQ=="},
                ]
            }
        },
    }
    result = ws_protocol.parse_sync_package(package)

    assert called["n"] == 0
    assert result == {"messages": [], "ack_pnm_ids": []}


def test_bare_ack_is_not_logged_as_unhandled(caplog):
    """心跳/ackDiff 的裸 ACK 不应产生 warn 级"未处理的消息类型"。"""
    client = _client()
    frames = [
        '{"headers": {"server-timestamp": 1793013663902}, "code": 200}',
        '{"headers": {"dt": "x", "mid": "m-1", "sid": "s"}, "code": 200}',
    ]
    with caplog.at_level(logging.DEBUG, logger="app.services.ws_client"):
        for frame in frames:
            asyncio.run(client._handle_message(frame, None))

    unhandled = [r for r in caplog.records if "未处理的消息类型" in r.getMessage()]
    assert unhandled == []


def test_unknown_lwp_still_warns(caplog):
    client = _client()
    with caplog.at_level(logging.WARNING, logger="app.services.ws_client"):
        asyncio.run(client._handle_message('{"lwp": "/x/unknown", "body": {}}', None))

    assert any("未处理的消息类型" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("phase", "expected"),
    [("closed", RECONNECT_DELAY), ("connected", RECONNECT_DELAY)],
)
def test_normal_session_end_reconnects_fast(phase, expected):
    client = _client()
    client.phase = phase
    assert client._next_reconnect_delay() == float(expected)


def test_failure_reconnect_backoff_is_exponential():
    client = _client()
    client.phase = "token_failed"
    delays = [client._next_reconnect_delay() for _ in range(5)]
    assert delays == [5.0, 10.0, 20.0, 40.0, 60.0]

    client.phase = "closed"
    assert client._next_reconnect_delay() == float(RECONNECT_DELAY)
