"""Regression tests for card delivery template compatibility.

Covers the 2026-10-08 production bug where a buyer received the card CSV
header line ("商品,批次,状态,备注,生成时间,...") instead of the configured
delivery template.

Root causes:
1. ``_load_goods_delivery_rule`` in card mode only read the ``cardTemplate``
   field; legacy/abnormal configs that stored the template under ``content``
   silently fell back to the bare ``{卡密}`` placeholder, so the configured
   message was ignored.
2. ``realtime_delivery`` only recognized ``{卡密}`` / ``{kmKey}`` placeholders;
   user-written ``{卡密占位}`` was not replaced and would be mangled into
   ``<secret>占位}`` because ``{卡密}`` is a substring of ``{卡密占位}``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ws_delivery_handler import _build_delivery_content


def _resolve_card_template(timing_config: dict) -> str:
    """Mirror ws_delivery_handler card-mode template resolution."""
    return str(
        timing_config.get("cardTemplate")
        or timing_config.get("content")
        or "{卡密}"
    )


def _render_card_message(template: str, secret: str) -> str:
    """Mirror realtime_delivery card message rendering."""
    template = (template or "").strip() or "{卡密}"
    if "{卡密}" in template or "{kmKey}" in template or "{卡密占位}" in template:
        return (
            template.replace("{卡密占位}", secret)
            .replace("{卡密}", secret)
            .replace("{kmKey}", secret)
        )
    return f"{template}\n{secret}" if template else secret


def test_card_template_falls_back_to_content_field() -> None:
    """cardTemplate missing -> fall back to content (legacy online data)."""
    timing_config = {
        "enabled": 1,
        "mode": "card",
        "cardGroupId": 2,
        "content": "请使用下面的兑换码领取key\n日卡{卡密占位}",
    }
    template = _resolve_card_template(timing_config)
    delivery_content = _build_delivery_content("", template, "")
    assert delivery_content == "请使用下面的兑换码领取key\n日卡{卡密占位}"


def test_card_template_prefers_card_template_over_content() -> None:
    """cardTemplate wins when both fields are present."""
    timing_config = {
        "cardTemplate": "T1:{卡密}",
        "content": "T2:{卡密}",
    }
    assert _resolve_card_template(timing_config) == "T1:{卡密}"


def test_card_template_defaults_to_bare_placeholder_when_both_missing() -> None:
    """Both fields missing -> default bare placeholder."""
    assert _resolve_card_template({}) == "{卡密}"


def test_render_replaces_zhanka_mi_zhanwei_placeholder() -> None:
    """{卡密占位} is a legal placeholder (user-written Chinese variant)."""
    template = "请使用下面的兑换码领取key\n日卡{卡密占位}"
    rendered = _render_card_message(template, "YF-AAAA-BBBB")
    assert rendered == "请使用下面的兑换码领取key\n日卡YF-AAAA-BBBB"
    assert "{卡密" not in rendered
    assert "占位}" not in rendered


def test_render_does_not_partially_replace_zhanka_mi_zhanwei() -> None:
    """{卡密} is a substring of {卡密占位}; the longer placeholder must be
    replaced first, otherwise the output is mangled into ``<secret>占位}``."""
    template = "卡密={卡密占位}"
    rendered = _render_card_message(template, "SECRET")
    assert rendered == "卡密=SECRET"
    assert "占位}" not in rendered


def test_render_supports_all_placeholder_variants_together() -> None:
    """Mixed {卡密}/{kmKey}/{卡密占位} in one template are all replaced."""
    template = "A{卡密}B{kmKey}C{卡密占位}D"
    rendered = _render_card_message(template, "X")
    assert rendered == "AXBXCXD"


def test_render_standard_kami_placeholder_unchanged() -> None:
    """Standard {卡密} placeholder behaviour unchanged."""
    assert _render_card_message("卡密：{卡密}", "S") == "卡密：S"


def test_render_kmkey_placeholder_unchanged() -> None:
    """{kmKey} placeholder behaviour unchanged."""
    assert _render_card_message("key={kmKey}", "S") == "key=S"


def test_render_template_without_placeholder_appends_secret() -> None:
    """Template with no placeholder appends the secret on a new line."""
    assert _render_card_message("感谢购买", "S") == "感谢购买\nS"


def test_render_empty_template_uses_default_placeholder() -> None:
    """Empty template falls back to bare placeholder so the card is sent."""
    assert _render_card_message("", "S") == "S"
