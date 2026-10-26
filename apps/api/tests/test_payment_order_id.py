"""付款卡片订单号解析 / 补发通道回归测试。

背景（2026-10-26 线上排查）：
``[我已付款，等待你发货]`` 卡片消息的 reminderUrl 不带 orderId，
订单号只在内层 dxCard JSON（``idleDeliver?...&orderId=``、``order_detail?id=``）
和 ``extJson.updateKey`` 里，导致 realtime_delivery_attempt.external_order_id 全为 NULL，
补单兜底也无法按订单幂等（账号 2 的已售出订单接口无权限，订单表为空）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import ws_protocol
from app.services.delivery_recovery import _message_from_chat_row
from app.services.payment_order_id import (
    extract_order_id_from_message,
    extract_order_id_from_payload,
    extract_order_id_from_text,
)

ORDER_ID = "5127385431272104524"


def _card_inner(with_order_id_param: bool = True) -> str:
    button_url = "https://h5.m.goofish.com/wow/moyu/moyu-project/idle-logistics/pages/idleDeliver"
    if with_order_id_param:
        button_url += f"?kun=true&titleVisible=false&orderId={ORDER_ID}"
    return json.dumps(
        {
            "contentType": 26,
            "dxCard": {
                "item": {
                    "main": {
                        "exContent": {
                            "button": {"targetUrl": button_url, "text": "去发货"},
                            "title": "我已付款，等待你发货",
                        },
                        "targetUrl": f"fleamarket://order_detail?id={ORDER_ID}&role=Seller",
                    }
                }
            },
        },
        ensure_ascii=False,
    )


def _payment_payload(with_order_id_param: bool = True) -> dict:
    return {
        "1": {
            "1": {"1": "2217666692269@goofish"},
            "2": "68536498773@goofish",
            "3": "4372161244173.PNM",
            "4": 0,
            "5": 1793014880352,
            "6": {
                "1": 101,
                "3": {
                    "1": "",
                    "2": "[我已付款，等待你发货]",
                    "3": "",
                    "4": 26,
                    "5": _card_inner(with_order_id_param),
                },
            },
            "10": {
                "reminderUrl": (
                    "fleamarket://message_chat?itemId=1091821311650&peerUserId=2217666692269"
                    "&sid=68536498773&messageId=885670367f594268bc1ac395847e0c3c&adv=no"
                ),
                "reminderContent": "[我已付款，等待你发货]",
                "reminderTitle": "等待你发货",
                "senderUserId": "2217666692269",
                "receiver": "488663297",
                "extJson": json.dumps(
                    {
                        "updateKey": f"68536498773:{ORDER_ID}:63:TRADE_PAID_DONE_SELLER:26",
                        "contentType": "26",
                    }
                ),
            },
            "12": 1,
        },
        "3": {"needPush": "true"},
    }


def test_extract_from_dxcard_button_url():
    payload = _payment_payload()
    assert extract_order_id_from_payload(payload) == ORDER_ID


def test_extract_from_order_detail_deeplink_only():
    """按钮 URL 没有 orderId 参数时，仍能从 fleamarket://order_detail?id= 取到。"""
    payload = _payment_payload(with_order_id_param=False)
    assert extract_order_id_from_payload(payload) == ORDER_ID


def test_extract_from_update_key_text():
    text = '{"updateKey":"68536498773:5127385431272104524:63:TRADE_PAID_DONE_SELLER:26"}'
    assert extract_order_id_from_text(text) == ORDER_ID


def test_no_false_positive_on_plain_chat_message():
    payload = {
        "1": {
            "1": {"1": "123@goofish"},
            "2": "68536498773@goofish",
            "3": "4372161244000.PNM",
            "5": 1793014880000,
            "6": {"1": 101, "3": {"1": "在吗", "2": "在吗", "4": 1}},
        }
    }
    assert extract_order_id_from_payload(payload) == ""


def test_parse_numbered_fields_includes_order_id():
    parsed = ws_protocol.parse_numbered_fields(_payment_payload())
    assert parsed is not None
    assert parsed["orderId"] == ORDER_ID
    assert parsed["sId"] == "68536498773@goofish"
    assert parsed["xyGoodsId"] == "1091821311650"


def test_chat_row_rebuild_keeps_order_id():
    """补发通道从 xianyu_chat_message 行还原订单号（历史数据也带 complete_msg）。"""
    msg = ws_protocol.parse_numbered_fields(_payment_payload())
    row = {
        "message_id": 1058,
        "account_id": 2,
        "s_id": "68536498773@goofish",
        "pnm_id": "4372161244173.PNM",
        "message_uid": "4372161244173.PNM",
        "sender_user_id": "2217666692269@goofish",
        "sender_user_name": "等待你发货",
        "xy_goods_id": "1091821311650",
        "reminder_content": "[我已付款，等待你发货]",
        "reminder_url": msg["reminderUrl"],
        "complete_msg": json.dumps(msg, ensure_ascii=False),
        "message_time": 1793014880352,
    }
    rebuilt = _message_from_chat_row(row)
    assert rebuilt["orderId"] == ORDER_ID
    assert rebuilt["sId"] == "68536498773@goofish"
    assert rebuilt["xyGoodsId"] == "1091821311650"
    assert rebuilt["pnmId"] == "4372161244173.PNM"


def test_extract_order_id_from_message_direct_key():
    assert extract_order_id_from_message({"orderId": ORDER_ID}) == ORDER_ID
    assert extract_order_id_from_message({"complete_msg": json.dumps({"orderId": ORDER_ID})}) == ORDER_ID
