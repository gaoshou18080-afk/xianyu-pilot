"""从闲鱼付款卡片消息中提取订单号 ``orderId``。

背景（2026-10-26 线上排查）：
``[我已付款，等待你发货]`` 这类 contentType=26 的系统卡片，
``reminderUrl`` 只带 ``itemId`` / ``peerUserId`` / ``sid``，**不带订单号**。
订单号藏在卡片内层 JSON 里：

- ``data["6"]["3"]["5"]`` 的转义 JSON 字符串（dxCard 模板内容）
  - ``dxCard.item.main.exContent.button.targetUrl`` → ``...idleDeliver?...&orderId=5127...``
  - ``dxCard.item.main.targetUrl`` → ``fleamarket://order_detail?id=5127...&role=Seller``
- ``data["10"]["extJson"]`` → ``updateKey":"<sid>:<orderId>:63:TRADE_PAID_DONE_SELLER:26"``

没有 orderId 时：

- 实时发货 event_key 只能退化成"消息事件键"（pnmId），无法按订单幂等；
- 补单兜底无法按订单去重，只能靠"会话 + 商品 + 时间窗"，同买家复购会被误判重复；
- ``realtime_delivery_attempt.external_order_id`` 全为 NULL（线上 34 条尝试全部如此）。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 闲鱼订单号是 16~20 位纯数字；这里放宽到 10~25 位避免漏掉历史短号。
_MIN_LEN = 10
_MAX_LEN = 25

_ORDER_ID_PATTERNS = (
    re.compile(rf"[?&]orderId=(\d{{{_MIN_LEN},{_MAX_LEN}}})"),
    re.compile(rf"[?&]bizOrderId=(\d{{{_MIN_LEN},{_MAX_LEN}}})"),
    re.compile(rf"order_detail\?id=(\d{{{_MIN_LEN},{_MAX_LEN}}})"),
    re.compile(rf'"updateKey"\s*:\s*"[^"]*?:(\d{{{_MIN_LEN},{_MAX_LEN}}}):'),
    re.compile(rf'"orderId"\s*:\s*"?(\d{{{_MIN_LEN},{_MAX_LEN}}})'),
)

# 已经解析好的消息字典里可能直接带订单号字段
_DIRECT_KEYS = (
    "orderId",
    "order_id",
    "externalOrderId",
    "external_order_id",
    "bizOrderId",
)

# 需要继续深挖的嵌套载荷字段（可能是 dict 也可能是 JSON 字符串）
_NESTED_KEYS = (
    "complete_msg",
    "completeMsg",
    "rawPayload",
    "raw_payload",
    "rawPayloadParsed",
)


def _as_digits(value: Any) -> str:
    text = str(value or "").strip()
    if text.isdigit() and _MIN_LEN <= len(text) <= _MAX_LEN:
        return text
    return ""


def extract_order_id_from_text(text: str) -> str:
    """从任意文本（URL / 卡片 JSON / 事件 JSON）中提取订单号。"""

    if not text:
        return ""
    for pattern in _ORDER_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            found = _as_digits(match.group(1))
            if found:
                return found
    return ""


def _dump(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _load_json_maybe(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _search_direct_keys(payload: Any, depth: int = 0) -> str:
    """在嵌套字典中按关键字找订单号（最多下钻 6 层）。"""

    if depth > 6:
        return ""
    if isinstance(payload, str):
        loaded = _load_json_maybe(payload)
        if loaded is None:
            return ""
        return _search_direct_keys(loaded, depth + 1)
    if isinstance(payload, dict):
        for key in _DIRECT_KEYS:
            if key in payload:
                found = _as_digits(payload.get(key))
                if found:
                    return found
        for value in payload.values():
            if isinstance(value, (dict, list, str)):
                found = _search_direct_keys(value, depth + 1)
                if found:
                    return found
        return ""
    if isinstance(payload, list):
        for value in payload[:50]:
            found = _search_direct_keys(value, depth + 1)
            if found:
                return found
    return ""


def extract_order_id_from_payload(payload: Any) -> str:
    """从原始 WS 字段树中提取订单号。"""

    found = _search_direct_keys(payload)
    if found:
        return found
    return extract_order_id_from_text(_dump(payload))


def extract_order_id_from_message(msg: Any) -> str:
    """从已解析的消息字典（或原始字段树）中提取订单号。"""

    if msg is None:
        return ""
    if not isinstance(msg, dict):
        return extract_order_id_from_payload(msg)

    found = _search_direct_keys(
        {key: msg[key] for key in _DIRECT_KEYS if key in msg}
    )
    if found:
        return found

    for key in _NESTED_KEYS:
        nested = msg.get(key)
        if nested in (None, "", {}, []):
            continue
        found = extract_order_id_from_payload(nested)
        if found:
            return found

    # 兜底：reminderUrl / 原始字段树里偶尔也会直接带 orderId
    return extract_order_id_from_payload(msg)
