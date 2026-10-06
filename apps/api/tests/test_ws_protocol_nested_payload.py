from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import msgpack

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ws_protocol import decrypt_payload, parse_sync_package


def _encoded_message() -> str:
    embedded_content = {
        "1": 101,
        "3": {
            "1": "",
            "2": "所以知道",
            "3": "",
            "4": 1,
            "5": json.dumps(
                {
                    "atUsers": [],
                    "contentType": 1,
                    "text": {"text": "所以知道"},
                },
                ensure_ascii=False,
            ),
        },
    }
    message = {
        "1": {
            "1": {"1": "2207712615459@goofish"},
            "2": "67432631607@goofish",
            "3": "4340953767026.PNM",
            "4": 0,
            "5": 1791277772673,
            "6": embedded_content,
            "7": 2,
            "8": 1,
            "9": 0,
            "10": {
                "itemId": "1085707255590",
                "senderUserId": "2207712615459",
                "senderUserName": "财团熊熊",
                "reminderContent": "所以知道",
            },
            "12": 1,
        },
        "3": {"needPush": "true"},
    }
    packed = msgpack.packb(message, use_bin_type=True)
    return base64.b64encode(packed).decode("ascii")


def test_embedded_content_objects_are_not_collected_as_messages():
    payloads = decrypt_payload(_encoded_message())

    assert len(payloads) == 1
    assert payloads[0]["1"]["3"] == "4340953767026.PNM"


def test_outer_message_is_parsed_without_duplicate_failed_message():
    result = parse_sync_package(
        {
            "body": {
                "syncPushPackage": {
                    "data": [{"data": _encoded_message()}],
                }
            }
        }
    )

    assert result is not None
    assert len(result["messages"]) == 1
    message = result["messages"][0]
    assert message["sId"] == "67432631607@goofish"
    assert message["pnmId"] == "4340953767026.PNM"
    assert message["senderUserId"] == "2207712615459@goofish"
    assert message["msgContent"] == "所以知道"
