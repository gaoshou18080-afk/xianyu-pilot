"""Event-driven, duplicate-resistant real-time automatic delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import async_session
from .realtime_delivery import (
    RealtimeDeliveryCommand,
    RealtimeDeliveryCoordinator,
    RealtimeDeliveryOutcome,
    SqlRealtimeDeliveryStore,
    XianyuRealtimeDeliveryGateway,
    build_realtime_delivery_event_key,
)
from .payment_order_id import extract_order_id_from_message
from .ws_client import ws_manager


logger = logging.getLogger(__name__)

def _normalize_segments_local(raw: Any) -> list[dict[str, Any]]:
    """规范化 segments（发货执行端版本，宽松校验：非法段跳过而非抛错）。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    elif isinstance(raw, list):
        parsed = raw
    else:
        return []
    if not isinstance(parsed, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        seg_type = str(item.get("type") or "").strip().lower()
        if seg_type == "text":
            content = str(item.get("content") or "").strip()
            if content:
                normalized.append({"type": "text", "content": content})
        elif seg_type == "image":
            image_url = str(item.get("imageUrl") or item.get("image_url") or "").strip()
            if image_url:
                seg: dict[str, Any] = {"type": "image", "imageUrl": image_url}
                asset_id = item.get("assetId") or item.get("asset_id")
                if asset_id is not None:
                    seg["assetId"] = asset_id
                normalized.append(seg)
    return normalized


async def _execute_segments_delivery(
    client: Any,
    cid: str,
    to_id: str,
    segments: list[dict[str, Any]],
) -> tuple[bool, str]:
    """按 segments 顺序逐条发送消息（文本/图片），严禁合并为一条。"""
    if not segments:
        return True, ""
    sent_count = 0
    total = len(segments)
    for idx, seg in enumerate(segments):
        seg_type = seg.get("type")
        try:
            if seg_type == "text":
                content = seg.get("content") or ""
                if not content:
                    continue
                await client.send_text_message(cid=cid, to_id=to_id, text=content)
                sent_count += 1
                if idx < total - 1:
                    await asyncio.sleep(0.5)
            elif seg_type == "image":
                image_url = seg.get("imageUrl") or seg.get("image_url") or ""
                if not image_url:
                    continue
                asset_id = seg.get("assetId") or seg.get("asset_id")
                send_image = getattr(client, "send_image_message", None)
                if callable(send_image):
                    await send_image(cid=cid, to_id=to_id, image_url=image_url, asset_id=asset_id)
                else:
                    await client.send_text_message(cid=cid, to_id=to_id, text=f"[图片] {image_url}")
                sent_count += 1
                if idx < total - 1:
                    await asyncio.sleep(0.5)
        except Exception as exc:
            logger.warning(
                "segments delivery failed at idx=%d type=%s error=%s",
                idx, seg_type, exc, exc_info=True,
            )
            return False, f"第 {idx + 1} 段发送失败：{exc}"
    if sent_count == 0:
        return False, "所有段均无有效内容"
    return True, ""


MODE_TEXT = "text"
MODE_CARD = "card"
MODE_KAMI = "kami"
MODE_CUSTOM = "custom"
MODE_API = "api"
DELIVERY_TIMING_AFTER_PAYMENT = "after_payment"
_SAFE_LOG_CODE_RE = re.compile(r"[^a-z0-9_]+")
_MAX_REALTIME_QUANTITY = 100

# 付款触发节流：防止闲鱼 WS 周期性推送付款消息导致重复触发发货（事故级 Bug 修复）
# key: f"{account_id}:{source_event_id}"，value: 上次触发时间戳
_payment_trigger_throttle: dict[str, float] = {}
_PAYMENT_THROTTLE_SECONDS = 60  # 同一 account_id + pnm_id 在 60 秒内只触发一次

# 付款事件正向关键词：reminder 文本命中任一即视为已付款
_PAYMENT_POSITIVE_KEYWORDS = (
    "等待你发货",
    "已付款",
    "买家已付款",
    "买家付款成功",
    "付款成功",
    "支付成功",
    "已支付",
    "买家已支付",
    "等待发货",
    "待发货",
    "买家已拍下并付款",
)
# 付款事件负向关键词：命中任一即视为未付款（优先级高于正向）
_PAYMENT_NEGATIVE_KEYWORDS = (
    "待付款",
    "拍下待付",
    "待买家付款",
    "等待买家付款",
    "未付款",
    "尚未付款",
)


def extract_order_id_from_url(url: str) -> Optional[str]:
    """Extract a non-empty order identifier from a reminder URL."""

    if not url:
        return None
    query = parse_qs(urlparse(url).query or "")
    for key in ("orderId", "tradeId", "id"):
        for value in query.get(key) or []:
            normalized = str(value).strip()
            if normalized:
                return normalized
    return None


def extract_goods_id_from_url(url: str) -> Optional[str]:
    """Extract a non-empty marketplace goods identifier from a reminder URL."""

    if not url:
        return None
    query = parse_qs(urlparse(url).query or "")
    for key in ("itemId", "id"):
        for value in query.get(key) or []:
            normalized = str(value).strip()
            if normalized:
                return normalized
    return None


def extract_peer_user_id_from_url(url: str) -> Optional[str]:
    """Extract the chat peer identifier without logging or persisting the URL."""

    if not url:
        return None
    query = parse_qs(urlparse(url).query or "")
    for key in ("peerUserId", "buyerUserId", "toId"):
        for value in query.get(key) or []:
            normalized = str(value).strip()
            if normalized:
                return normalized
    return None


def extract_buy_quantity_from_msg(msg: dict) -> int:
    complete_msg = msg.get("complete_msg") or msg.get("completeMsg") or {}
    if isinstance(complete_msg, str):
        try:
            complete_msg = json.loads(complete_msg)
        except (json.JSONDecodeError, TypeError):
            complete_msg = {}
    if isinstance(complete_msg, dict):
        quantity = complete_msg.get("quantity") or complete_msg.get("buyQuantity")
        try:
            if quantity is not None:
                return max(int(quantity), 1)
        except (TypeError, ValueError):
            pass

    reminder = str(msg.get("reminderContent") or msg.get("reminder_content") or "")
    match = re.search(r"[x×](\d+)", reminder)
    if match:
        try:
            return max(int(match.group(1)), 1)
        except (TypeError, ValueError):
            pass
    return 1


def is_payment_message(msg: dict) -> bool:
    """仅在买家真正付款后才返回 True，避免"拍下待付款"等未付款状态触发发货。

    闲鱼订单系统通知（contentType=26）覆盖"拍下待付款""已付款""确认收货"等
    多种状态。把所有 contentType=26 一律视为付款，会导致买家拍下未付款就发货，
    并在真正付款后再发一次。这里显式排除"待付款"，并要求提醒文本明确表示已付款。
    """
    content_type = msg.get("contentType") or msg.get("content_type") or 0
    try:
        content_type_int = int(content_type)
    except (TypeError, ValueError):
        content_type_int = 0
    reminder = str(msg.get("reminderContent") or msg.get("reminder_content") or "")
    reminder_preview = reminder[:120]

    # 负向关键词优先：明确表示未付款，必须跳过
    for neg in _PAYMENT_NEGATIVE_KEYWORDS:
        if neg in reminder:
            logger.info(
                "is_payment_message denied by negative keyword='%s' contentType=%d reminder=%s",
                neg, content_type_int, reminder_preview,
            )
            return False

    # 正向关键词：明确表示已付款或等待发货
    for pos in _PAYMENT_POSITIVE_KEYWORDS:
        if pos in reminder:
            logger.info(
                "is_payment_message accepted by positive keyword='%s' contentType=%d",
                pos, content_type_int,
            )
            return True

    logger.debug(
        "is_payment_message denied no keyword matched contentType=%d reminder=%s",
        content_type_int, reminder_preview,
    )
    return False


def is_bargain_success_message(msg: dict) -> bool:
    reminder = str(msg.get("reminderContent") or msg.get("reminder_content") or "")
    return "小刀成功" in reminder or "我已成功小刀" in reminder


def is_bargain_waiting_message(msg: dict) -> bool:
    reminder = str(msg.get("reminderContent") or msg.get("reminder_content") or "")
    return "小刀" in reminder and "待刀成" in reminder


async def _handle_bargain_waiting_message(
    account_id: int,
    msg: dict,
) -> None:
    """处理"我已小刀，待刀成"消息：标记小刀订单 + 调用免拼接口。

    买家发起小刀后，系统自动调用免拼接口（mtop.idle.groupon.activity.seller.freeshipping）
    促成小刀成交。此步骤不发送发货信息，完整发货由后续"小刀成功"消息触发。
    """
    s_id = str(msg.get("sId") or "")
    reminder_url = str(msg.get("reminderUrl") or msg.get("reminder_url") or "")

    order_id = extract_order_id_from_url(reminder_url)
    xy_goods_id = str(msg.get("xyGoodsId") or "")
    if not xy_goods_id:
        xy_goods_id = extract_goods_id_from_url(reminder_url) or ""
    buyer_user_id = str(msg.get("senderUserId") or "")
    if not buyer_user_id:
        buyer_user_id = extract_peer_user_id_from_url(reminder_url) or ""

    logger.info(
        "检测到待刀成消息: accountId=%d orderId=%s xyGoodsId=%s buyer=%s sId=%s",
        account_id, order_id, xy_goods_id, buyer_user_id, s_id,
    )

    if not xy_goods_id or not buyer_user_id:
        logger.warning(
            "待刀成消息缺少商品ID或买家ID，跳过免拼: accountId=%d xyGoodsId=%s buyer=%s",
            account_id, xy_goods_id, buyer_user_id,
        )
        return

    async with async_session() as db:
        try:
            # 1. 标记订单为小刀订单
            if order_id:
                await _mark_order_as_bargain(db, account_id, order_id, xy_goods_id)

            # 2. 商品归属检查：若商品不属于本账号或未配置发货规则，则跳过免拼
            rule = await _match_delivery_rule(db, account_id, xy_goods_id)
            if not rule:
                logger.info(
                    "待刀成消息：商品不属于本账号或未配置发货规则，跳过免拼: accountId=%d xyGoodsId=%s",
                    account_id, xy_goods_id,
                )
                await db.commit()
                return

            # 3. 调用免拼接口（freeshipping_order 已支持幂等）
            from .xianyu_api_service import freeshipping_order
            result = await asyncio.to_thread(
                freeshipping_order,
                account_id, order_id or "", xy_goods_id, buyer_user_id,
            )

            if result and result.get("success"):
                logger.info(
                    "待刀成消息：免拼接口调用成功: accountId=%d orderId=%s itemId=%s buyerId=%s",
                    account_id, order_id, xy_goods_id, buyer_user_id,
                )
            else:
                logger.warning(
                    "待刀成消息：免拼接口调用失败: accountId=%d orderId=%s error=%s",
                    account_id, order_id,
                    result.get("error", "") if result else "NONE",
                )

            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.error(
                "待刀成消息处理失败: accountId=%d error=%s",
                account_id, e, exc_info=True,
            )


async def _mark_order_as_bargain(
    db: AsyncSession,
    account_id: int,
    order_id: str,
    xy_goods_id: str,
) -> None:
    """标记订单为小刀订单（is_bargain=1）。

    优先按 external_order_id 精确匹配；若未命中（订单可能尚未同步），
    按 account_id + external_goods_id 匹配最近一笔订单兜底。
    """
    # 1. 按 external_order_id 精确匹配
    result = await db.execute(
        text("""
            UPDATE xianyu_trade_order
            SET is_bargain = 1, updated_time = NOW()
            WHERE account_id = :account_id
              AND external_order_id = :external_order_id
              AND deleted = 0
        """),
        {
            "account_id": account_id,
            "external_order_id": order_id,
        },
    )

    # 2. 若未命中，按 xy_goods_id 兜底
    if result.rowcount == 0 and xy_goods_id:
        await db.execute(
            text("""
                UPDATE xianyu_trade_order
                SET is_bargain = 1, updated_time = NOW()
                WHERE account_id = :account_id
                  AND external_goods_id = :xy_goods_id
                  AND deleted = 0
                  AND id = (
                      SELECT id FROM (
                          SELECT id FROM xianyu_trade_order
                          WHERE account_id = :account_id
                            AND external_goods_id = :xy_goods_id
                            AND deleted = 0
                          ORDER BY created_time DESC
                          LIMIT 1
                      ) AS t
                  )
            """),
            {
                "account_id": account_id,
                "xy_goods_id": xy_goods_id,
            },
        )


async def handle_incoming_message_for_delivery(
    account_id: int,
    msg: dict,
) -> None:
    """Process one stored WebSocket event through the durable state machine."""

    # "待刀成"消息：标记小刀订单 + 调免拼接口（促成小刀成交），不触发完整发货
    if is_bargain_waiting_message(msg):
        await _handle_bargain_waiting_message(account_id, msg)
        return

    payment_event = is_payment_message(msg)
    bargain_event = is_bargain_success_message(msg)
    if not payment_event and not bargain_event:
        # 记录跳过原因，便于排查"收到付款通知但未触发发货"
        reminder = str(msg.get("reminderContent") or msg.get("reminder_content") or "")
        content_type = msg.get("contentType") or msg.get("content_type") or 0
        logger.debug(
            "delivery event skipped accountId=%d payment=%s bargain=%s contentType=%s reminder=%s",
            account_id, payment_event, bargain_event, content_type, reminder[:80],
        )
        return

    # 付款兜底节流：同一 account_id + pnm_id 在 60 秒内只触发一次，防止 WS 周期性推送死循环
    if payment_event and _should_skip_payment_trigger(account_id, msg):
        logger.debug(
            "付款触发节流跳过 accountId=%d pnmId=%s（60秒内已触发）",
            account_id,
            str(msg.get("pnmId") or msg.get("pnm_id") or "")[:32],
        )
        return

    logger.info(
        "Realtime delivery event accepted accountId=%d payment=%s bargain=%s",
        account_id, payment_event, bargain_event,
    )
    async with async_session() as db:
        try:
            outcome = await _process_delivery(db, account_id, msg)
            if outcome is not None and payment_event and not outcome.repeated:
                try:
                    from .notify_dispatcher import notify_new_order

                    await notify_new_order(account_id, msg)
                except Exception as exc:
                    logger.warning(
                        "New-order notification failed accountId=%d errorType=%s",
                        account_id,
                        type(exc).__name__,
                    )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            error_code = _safe_log_code(getattr(exc, "error_code", "delivery_handler_failed"))
            logger.error(
                "Realtime delivery stopped accountId=%d errorCode=%s errorType=%s",
                account_id,
                error_code,
                type(exc).__name__,
            )


_ORDER_DELIVERY_LOCKS: dict[str, asyncio.Lock] = {}


def _delivery_lock(key: str) -> asyncio.Lock:
    """取（或创建）某个账号+订单的进程内串行锁。"""
    lock = _ORDER_DELIVERY_LOCKS.get(key)
    if lock is None:
        if len(_ORDER_DELIVERY_LOCKS) > 512:
            # 简单防膨胀：清空比泄漏锁对象更安全（锁只在极短窗口内有意义）
            _ORDER_DELIVERY_LOCKS.clear()
        lock = asyncio.Lock()
        _ORDER_DELIVERY_LOCKS[key] = lock
    return lock


async def _process_delivery(
    db: AsyncSession,
    account_id: int,
    msg: dict,
) -> RealtimeDeliveryOutcome | None:
    """同一账号+同一订单的并发触发串行化。

    背景（2026-10-29 实测）：一单付款会连着收到 2 条付款相关消息，两条几乎同时
    进入发货流程。两次去重查询都在对方写库前完成，于是同一订单建了两个 attempt
    （47 failed unconfigured + 48 success）。虽然最终只发了一张卡密，但留下了
    噪声 attempt 与重复告警。这里按 orderId 串行化：第二个触发会等第一个结束后
    再走一遍去重，从而直接跳过。
    """
    order_key = (
        str(msg.get("orderId") or msg.get("order_id") or "").strip()
        or extract_order_id_from_message(msg)
        or ""
    )
    if not order_key:
        return await _process_delivery_inner(db, account_id, msg)

    lock = _delivery_lock(f"{account_id}:{order_key}")
    if lock.locked():
        logger.info(
            "同订单发货已在处理中，等待串行化 accountId=%d orderId=%s",
            account_id, order_key,
        )
    try:
        await asyncio.wait_for(lock.acquire(), timeout=120)
    except asyncio.TimeoutError:
        logger.warning(
            "同订单发货串行化等待超时，跳过本次触发 accountId=%d orderId=%s",
            account_id, order_key,
        )
        return None
    try:
        return await _process_delivery_inner(db, account_id, msg)
    finally:
        lock.release()


async def _process_delivery_inner(
    db: AsyncSession,
    account_id: int,
    msg: dict,
) -> RealtimeDeliveryOutcome | None:
    session_id = str(msg.get("sId") or msg.get("sid") or "").strip()
    reminder_url = str(msg.get("reminderUrl") or msg.get("reminder_url") or "")
    # 订单号优先取解析出来的 orderId（付款卡片 dxCard 内层 JSON 才有），
    # reminderUrl 通常不带 orderId，只作为兜底。
    external_order_id = (
        str(msg.get("orderId") or msg.get("order_id") or "").strip()
        or extract_order_id_from_message(msg)
        or extract_order_id_from_url(reminder_url)
    )
    item_id = str(msg.get("xyGoodsId") or "").strip()
    if not item_id:
        item_id = extract_goods_id_from_url(reminder_url) or ""
    peer_id = str(msg.get("senderUserId") or "").strip()
    if not peer_id:
        peer_id = extract_peer_user_id_from_url(reminder_url) or ""
    source_event_id = _extract_source_event_id(msg)
    if not source_event_id and external_order_id:
        source_event_id = f"order:{external_order_id}"

    missing = [
        name
        for name, value in (
            ("session", session_id),
            ("item", item_id),
            ("peer", peer_id),
            ("sourceEvent", source_event_id),
        )
        if not value
    ]
    if missing:
        logger.warning(
            "Realtime delivery denied accountId=%d missingContext=%s reminderUrl=%s itemId=%s peerId=%s",
            account_id,
            ",".join(missing),
            reminder_url[:120],
            item_id,
            peer_id,
        )
        return None

    quantity = extract_buy_quantity_from_msg(msg)
    if quantity > _MAX_REALTIME_QUANTITY:
        logger.warning(
            "Realtime delivery denied accountId=%d errorCode=quantity_limit_exceeded quantity=%d",
            account_id,
            quantity,
        )
        return None

    event_key = build_realtime_delivery_event_key(
        account_id=account_id,
        external_order_id=external_order_id,
        source_event_id=source_event_id,
        session_id=session_id,
        item_id=item_id,
    )

    # 数据库层去重第一道防线：按订单号查 realtime_delivery_attempt（订单维度最可靠），
    # 没有订单号时退化成"会话 + 商品 + 买家 + 时间窗"。
    # 背景：旧实现查 delivery_record.receiver_info 里的 sid/xyGoodsId/buyerUserId，
    # 但实时发货写库时 receiver_info 只有 {"realtimeAttemptId": N}，该查询永远命中 0 行，
    # 等于这层防线失效。
    duplicate_reason = await _find_duplicate_delivery(
        db,
        account_id=account_id,
        external_order_id=external_order_id,
        session_id=session_id,
        item_id=item_id,
        peer_id=peer_id,
    )
    if duplicate_reason:
        logger.info(
            "去重命中，跳过发货 accountId=%d orderId=%s itemId=%s sid=%s reason=%s",
            account_id, external_order_id, item_id, session_id[:32], duplicate_reason,
        )
        return None

    # 数据库层去重第二道防线：xianyu_trade_order.order_status=3（已发货）
    if external_order_id and await _check_order_already_shipped(db, account_id, external_order_id):
        logger.info(
            "订单已发货（order_status=3），跳过发货 accountId=%d orderId=%s",
            account_id, external_order_id,
        )
        return None

    rule = await resolve_realtime_delivery_rule(
        db,
        account_id=account_id,
        external_goods_id=item_id,
    )
    if not rule:
        reason = await _diagnose_missing_rule(db, account_id, item_id)
        logger.warning(
            "Realtime delivery no rule matched accountId=%d itemId=%s orderId=%s reason=%s",
            account_id, item_id, external_order_id, reason,
        )
    else:
        logger.info(
            "Realtime delivery rule matched accountId=%d itemId=%s ruleId=%s mode=%s",
            account_id, item_id, rule.get("id"), rule.get("delivery_mode"),
        )

    # 多规格 SKU 规则匹配：若商品配置了 skuRules，则反查订单 skuId 并按 SKU 精确匹配发货规则。
    # 反查失败或未匹配时回退到商品通用配置（保证发货不中断）。
    sku_rules = rule.get("_sku_rules") if isinstance(rule, dict) else None
    if sku_rules and external_order_id:
        sku_id = await _resolve_order_sku_id(db, account_id, external_order_id, item_id)
        if sku_id:
            rule = await _apply_sku_rule_override(db, rule, sku_id)
            logger.info(
                "SKU规则匹配 accountId=%d xyGoodsId=%s skuId=%s matched=%s",
                account_id, item_id, sku_id, rule.get("_sku_matched"),
            )
        else:
            logger.info(
                "SKU反查失败，回退商品通用配置 accountId=%d orderId=%s",
                account_id, external_order_id,
            )

    mode = _normalize_delivery_mode(rule.get("delivery_mode") if rule else "unconfigured")
    content = str((rule or {}).get("delivery_content") or "")
    content = _render_delivery_content(
        content,
        buyer_name=str(msg.get("senderUserName") or ""),
        external_order_id=external_order_id,
    )
    # 发货声明流程：若声明开关开启，发送声明并创建 waiting 会话，不立即发货
    try:
        from .ws_statement_handler import should_send_statement
        if await should_send_statement(db, account_id):
            from .ws_statement_handler import send_statement_and_create_session
            statement_sent = await send_statement_and_create_session(
                db,
                account_id=account_id,
                msg=msg,
                order_id=external_order_id,
                xy_goods_id=item_id,
                s_id=session_id,
                pnm_id=source_event_id,
                buyer_user_id=peer_id,
                buyer_user_name=str(msg.get("senderUserName") or ""),
                goods_title=str((rule or {}).get("source_title") or ""),
            )
            if statement_sent:
                logger.info(
                    "声明已发送，暂停实时发货 accountId=%d orderId=%s",
                    account_id, external_order_id,
                )
                return None
    except Exception as exc:
        logger.warning(
            "声明流程异常，回退到直接发货 accountId=%d errorType=%s",
            account_id, type(exc).__name__,
        )
    command = RealtimeDeliveryCommand(
        event_key=event_key,
        account_id=account_id,
        external_order_id=external_order_id,
        source_event_id=source_event_id,
        session_id=session_id,
        peer_id=peer_id,
        item_id=item_id,
        rule_id=_optional_int((rule or {}).get("id")),
        delivery_mode=mode,
        delivery_content=content,
        quantity_requested=quantity,
        card_group_id=_optional_int((rule or {}).get("card_group_id")),
        auto_confirm_shipment=_truthy((rule or {}).get("auto_confirm_shipment")),
        segments=(rule or {}).get("segments") or None,
    )
    coordinator = RealtimeDeliveryCoordinator(
        store=SqlRealtimeDeliveryStore(db),
        gateway=XianyuRealtimeDeliveryGateway(),
    )
    outcome = await coordinator.execute(command)
    logger.info(
        "Realtime delivery state accountId=%d attemptId=%d state=%s errorCode=%s",
        account_id,
        outcome.attempt_id,
        outcome.status,
        _safe_log_code(outcome.error_code or "none"),
    )
    if outcome.status in {"failed", "unknown", "message_sent"}:
        await _notify_realtime_delivery_attention(db, account_id, outcome)
    return outcome


async def resolve_realtime_delivery_rule(
    db: AsyncSession,
    *,
    account_id: int,
    external_goods_id: str,
) -> Optional[dict]:
    """Resolve a delivery rule without ever crossing the account seam."""

    return await _match_delivery_rule(db, account_id, external_goods_id)


async def _match_delivery_rule(
    db: AsyncSession,
    account_id: int,
    external_goods_id: str,
) -> Optional[dict]:
    goods = await _find_goods_for_delivery(db, account_id, external_goods_id)
    if goods:
        goods_rule = await _load_goods_delivery_rule(db, goods)
        if goods_rule:
            return goods_rule

    local_goods_id = int(goods["id"]) if goods and goods.get("id") is not None else 0
    rows = (
        await db.execute(
            text(
                """
                SELECT id, account_id, goods_id, rule_name, delivery_mode,
                       card_group_id, delivery_content, status
                FROM delivery_rule
                WHERE deleted = 0
                  AND status = 1
                  AND (account_id IS NULL OR account_id = :account_id)
                  AND (goods_id IS NULL OR goods_id = 0 OR goods_id = :goods_id)
                ORDER BY
                  CASE
                    WHEN account_id = :account_id AND goods_id = :goods_id THEN 0
                    WHEN account_id = :account_id AND (goods_id IS NULL OR goods_id = 0) THEN 1
                    WHEN account_id IS NULL AND goods_id = :goods_id THEN 2
                    ELSE 3
                  END,
                  id DESC
                LIMIT 1
                """
            ),
            {"account_id": account_id, "goods_id": local_goods_id},
        )
    ).mappings().all()
    if not rows:
        return None
    rule = dict(rows[0])
    rule["delivery_mode"] = _normalize_delivery_mode(rule.get("delivery_mode"))
    # 旧版 delivery_rule 表无 auto_confirm_shipment 字段，无法自动确认发货；
    # 自动确认发货仅在 delivery_goods_config（新版配置）路径下生效。
    rule["auto_confirm_shipment"] = 0
    return rule


async def _diagnose_missing_rule(
    db: AsyncSession,
    account_id: int,
    external_goods_id: str,
) -> str:
    """规则未匹配时给出可读原因，避免"我明明启用了却静默不发货"。

    返回：goods_not_found / config_missing / config_disabled /
    mode_unsupported / card_group_missing / legacy_rule_missing / unknown
    """

    try:
        goods = await _find_goods_for_delivery(db, account_id, external_goods_id)
        if not goods:
            return "goods_not_found"
        row = (
            await db.execute(
                text(
                    """
                    SELECT config_json
                    FROM delivery_goods_config
                    WHERE goods_id = :goods_id
                      AND deleted = 0
                    LIMIT 1
                    """
                ),
                {"goods_id": goods.get("id")},
            )
        ).mappings().first()
        if not row:
            return "config_missing"
        config = _json_object(row.get("config_json"))
        timing_config = config.get("payDelivery")
        if not isinstance(timing_config, dict):
            return "config_missing"
        if not _truthy(timing_config.get("enabled")):
            return "config_disabled"
        mode = _normalize_delivery_mode(timing_config.get("mode"))
        if mode not in {MODE_TEXT, MODE_CARD}:
            return "mode_unsupported"
        if mode == MODE_CARD and not _optional_int(timing_config.get("cardGroupId")):
            return "card_group_missing"
        return "unknown"
    except Exception:  # noqa: BLE001 - 诊断失败不影响主流程
        logger.debug("发货规则诊断失败 accountId=%d itemId=%s", account_id, external_goods_id, exc_info=True)
        return "unknown"


async def _find_goods_for_delivery(
    db: AsyncSession,
    account_id: int,
    external_goods_id: str,
) -> Optional[dict]:
    if not external_goods_id:
        return None
    row = (
        await db.execute(
            text(
                """
                SELECT id, account_id, external_goods_id, title
                FROM xianyu_goods
                WHERE deleted = 0
                  AND account_id = :account_id
                  AND external_goods_id = :external_goods_id
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {
                "account_id": account_id,
                "external_goods_id": external_goods_id,
            },
        )
    ).mappings().first()

    if not row:
        # 商品不存在时，尝试从订单项表补全最小商品记录，确保发货配置可命中。
        # 场景：WS 实时收到付款消息，但商品尚未同步到 xianyu_goods 表。
        await _ensure_goods_from_order_items(db, account_id, external_goods_id)
        row = (
            await db.execute(
                text(
                    """
                    SELECT id, account_id, external_goods_id, title
                    FROM xianyu_goods
                    WHERE deleted = 0
                      AND account_id = :account_id
                      AND external_goods_id = :external_goods_id
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ),
                {
                    "account_id": account_id,
                    "external_goods_id": external_goods_id,
                },
            )
        ).mappings().first()

    return dict(row) if row else None


async def _ensure_goods_from_order_items(
    db: AsyncSession,
    account_id: int,
    external_goods_id: str,
) -> bool:
    """实时发货时发现商品不存在，从订单项表补全最小商品记录。

    确保实时路径和批量路径都能在商品未同步时补全占位记录。
    """
    if not external_goods_id:
        return False

    # 从订单项表获取商品信息
    item_row = (
        await db.execute(
            text("""
                SELECT oi.goods_title, oi.goods_image, oi.goods_price
                FROM xianyu_trade_order_item oi
                JOIN xianyu_trade_order o ON o.id = oi.order_id
                WHERE oi.deleted = 0
                  AND oi.goods_id = :goods_id
                  AND o.account_id = :account_id
                ORDER BY oi.id DESC LIMIT 1
            """),
            {
                "account_id": account_id,
                "goods_id": int(external_goods_id) if external_goods_id.isdigit() else 0,
            },
        )
    ).mappings().first()

    title = ""
    image_url = ""
    price = "0"
    if item_row:
        title = str(item_row.get("goods_title") or "")
        image_url = str(item_row.get("goods_image") or "")
        price = str(item_row.get("goods_price") or "0")

    # 防御性查重：INSERT 前确认该商品在任意 deleted 状态下都不存在
    existing_row = (
        await db.execute(
            text("""
                SELECT id FROM xianyu_goods
                WHERE account_id = :account_id
                  AND external_goods_id = :external_goods_id
                LIMIT 1
            """),
            {
                "account_id": account_id,
                "external_goods_id": external_goods_id,
            },
        )
    ).mappings().first()
    if existing_row:
        return False

    try:
        await db.execute(
            text("""
                INSERT INTO xianyu_goods (
                    account_id, external_goods_id, goods_id, title,
                    price, sold_price, cover_pic, image_url, status,
                    deleted, created_time, updated_time
                ) VALUES (
                    :account_id, :external_goods_id, :goods_id, :title,
                    :price, :price, :image_url, :image_url, 1,
                    0, NOW(), NOW()
                )
            """),
            {
                "account_id": account_id,
                "external_goods_id": external_goods_id,
                "goods_id": external_goods_id,
                "title": (title or f"商品 {external_goods_id}")[:255],
                "price": (price or "0")[:32],
                "image_url": image_url or None,
            },
        )
        logger.info(
            "实时发货自动补全商品占位记录 accountId=%d externalGoodsId=%s",
            account_id, external_goods_id
        )
        return True
    except Exception:
        return False


async def _load_goods_delivery_rule(
    db: AsyncSession,
    goods: dict,
) -> Optional[dict]:
    row = (
        await db.execute(
            text(
                """
                SELECT id, goods_id, config_json
                FROM delivery_goods_config
                WHERE goods_id = :goods_id
                  AND deleted = 0
                LIMIT 1
                """
            ),
            {"goods_id": goods.get("id")},
        )
    ).mappings().first()
    if not row:
        return None

    config = _json_object(row.get("config_json"))
    timing_config = config.get("payDelivery")
    if not isinstance(timing_config, dict) or not _truthy(timing_config.get("enabled")):
        return None

    mode = _normalize_delivery_mode(timing_config.get("mode"))
    header = str(timing_config.get("header") or "")
    footer = str(timing_config.get("footer") or "")
    source_id = timing_config.get("sourceId")
    source_title = str(timing_config.get("sourceTitle") or "")
    if mode == MODE_TEXT:
        content = str(timing_config.get("content") or "")
        if source_id:
            source = await _load_text_source(db, source_id)
            if source:
                content = content or str(source.get("content") or "")
                source_title = source_title or str(source.get("title") or "")
        delivery_content = _build_delivery_content(header, content, footer)
        if not delivery_content.strip():
            return None
    elif mode == MODE_CARD:
        # 兼容旧数据：历史配置把卡密模板写在 content 字段，cardTemplate 缺失时回退到 content
        card_template = str(
            timing_config.get("cardTemplate")
            or timing_config.get("content")
            or "{卡密}"
        )
        delivery_content = _build_delivery_content(header, card_template, footer)
    else:
        # Persist the retired mode as failed; never read or call its URL fields.
        delivery_content = ""

    # 从货源读取 segments（多条正文+图片配置），用于多段发送
    source_segments = []
    if source_id:
        source = await _load_text_source(db, source_id)
        if source:
            source_segments = _normalize_segments_local(source.get("segments"))

    # 保留 SKU 规则列表，供 _apply_sku_rule_override 按 skuId 精确匹配
    sku_rules = config.get("skuRules") if isinstance(config.get("skuRules"), list) else []

    return {
        "id": row.get("id"),
        "goods_id": goods.get("id"),
        "delivery_mode": mode,
        "delivery_content": delivery_content,
        "delivery_timing": DELIVERY_TIMING_AFTER_PAYMENT,
        "source_id": source_id,
        "source_title": source_title,
        "card_group_id": timing_config.get("cardGroupId"),
        "auto_confirm_shipment": timing_config.get("autoConfirmShipment")
        or timing_config.get("auto_confirm_shipment")
        or 0,
        "segments": source_segments,
        "_sku_rules": sku_rules,
    }


async def _resolve_order_sku_id(
    db: AsyncSession,
    account_id: int,
    external_order_id: str,
    xy_goods_id: str,
) -> Optional[str]:
    """反查订单的 skuId，用于多规格商品按 SKU 匹配发货规则。

    查询路径：通过 external_order_id 关联 xianyu_trade_order.id，
    再从 xianyu_trade_order_item 表读取 sku_id。

    查询失败或无 sku_id 时返回 None（调用方回退商品通用配置）。
    """
    if not external_order_id:
        return None
    try:
        row = (
            await db.execute(
                text(
                    """
                    SELECT oi.sku_id
                    FROM xianyu_trade_order_item oi
                    JOIN xianyu_trade_order o ON o.id = oi.order_id
                    WHERE o.account_id = :account_id
                      AND o.external_order_id = :external_order_id
                      AND oi.deleted = 0
                      AND oi.sku_id IS NOT NULL
                      AND oi.sku_id != ''
                    LIMIT 1
                    """
                ),
                {
                    "account_id": account_id,
                    "external_order_id": external_order_id,
                },
            )
        ).mappings().first()
        if row and row.get("sku_id"):
            return str(row["sku_id"])
    except Exception as error:
        logger.debug(
            "查询订单项 sku_id 失败 accountId=%d orderId=%s error=%s",
            account_id, external_order_id, error,
        )
    return None


async def _lookup_sku_property_key(
    db: AsyncSession,
    goods_id: Any,
    sku_id: str,
) -> Optional[str]:
    """从 xianyu_goods_sku 表查询 sku_id 对应的 property_key。"""
    if not goods_id or not sku_id:
        return None
    try:
        row = (
            await db.execute(
                text(
                    """
                    SELECT property_key FROM xianyu_goods_sku
                    WHERE goods_id = :goods_id AND sku_id = :sku_id
                    LIMIT 1
                    """
                ),
                {"goods_id": goods_id, "sku_id": sku_id},
            )
        ).mappings().first()
        return str(row.get("property_key") or "") if row else None
    except Exception as error:
        logger.debug(
            "查询 SKU property_key 失败 goodsId=%s skuId=%s error=%s",
            goods_id, sku_id, error,
        )
    return None


async def _apply_sku_rule_override(
    db: AsyncSession,
    rule: dict,
    sku_id: str,
) -> dict:
    """按 skuId 在 skuRules 中查找精确规则，覆盖商品通用规则字段。

    匹配顺序：
    1. skuId 精确匹配
    2. propertyKey 二次匹配（从 xianyu_goods_sku 表反查 property_key 后比对）

    未匹配时返回原 rule（不修改）。
    匹配时重新加载货源 sourceId 以获取对应的 content/segments。
    """
    sku_rules = rule.get("_sku_rules") or []
    if not sku_rules or not sku_id:
        return rule

    matched = None
    for item in sku_rules:
        if not isinstance(item, dict):
            continue
        if str(item.get("skuId") or "") == str(sku_id):
            matched = item
            break

    if not matched:
        # skuId 未命中，反查 property_key 后按 propertyKey 二次匹配
        property_key = await _lookup_sku_property_key(db, rule.get("goods_id"), sku_id)
        if property_key:
            for item in sku_rules:
                if not isinstance(item, dict):
                    continue
                if str(item.get("propertyKey") or "") == property_key:
                    matched = item
                    break

    if not matched:
        return rule

    # 从匹配的 skuRule 的 payDelivery timing 配置中读取覆盖字段
    timing_config = matched.get("payDelivery")
    if not isinstance(timing_config, dict) or not _truthy(timing_config.get("enabled")):
        # SKU 规则未启用，回退通用配置
        return rule

    mode = _normalize_delivery_mode(timing_config.get("mode"))
    header = str(timing_config.get("header") or "")
    footer = str(timing_config.get("footer") or "")
    source_id = timing_config.get("sourceId")
    source_title = str(timing_config.get("sourceTitle") or "")

    if mode == MODE_TEXT:
        content = str(timing_config.get("content") or "")
        if source_id:
            source = await _load_text_source(db, source_id)
            if source:
                content = content or str(source.get("content") or "")
                source_title = source_title or str(source.get("title") or "")
        delivery_content = _build_delivery_content(header, content, footer)
        if not delivery_content.strip():
            return rule
    elif mode == MODE_CARD:
        # 兼容旧数据：cardTemplate 缺失时回退到 content（SKU 覆盖场景同商品通用规则）
        card_template = str(
            timing_config.get("cardTemplate")
            or timing_config.get("content")
            or "{卡密}"
        )
        delivery_content = _build_delivery_content(header, card_template, footer)
    else:
        delivery_content = ""

    # 从货源读取 segments
    source_segments: list[dict[str, Any]] = []
    if source_id:
        source = await _load_text_source(db, source_id)
        if source:
            source_segments = _normalize_segments_local(source.get("segments"))

    rule["delivery_mode"] = mode
    rule["delivery_content"] = delivery_content
    rule["card_group_id"] = timing_config.get("cardGroupId")
    rule["source_id"] = source_id
    rule["source_title"] = source_title
    rule["auto_confirm_shipment"] = (
        timing_config.get("autoConfirmShipment")
        or timing_config.get("auto_confirm_shipment")
        or 0
    )
    rule["segments"] = source_segments
    rule["_sku_matched"] = True
    return rule


async def _load_text_source(db: AsyncSession, source_id: Any) -> Optional[dict]:
    row = (
        await db.execute(
            text(
                """
                SELECT id, title, content, segments
                FROM delivery_text_source
                WHERE id = :source_id
                  AND deleted = 0
                LIMIT 1
                """
            ),
            {"source_id": source_id},
        )
    ).mappings().first()
    return dict(row) if row else None


async def _ensure_ws_connected(account_id: int, timeout: float = 12.0) -> bool:
    """确保账号的 WebSocket 已连接，未连接时自动从 DB 重启并等待注册完成。

    用于手动发货等需要即时发送消息的场景，避免因 WS 临时断开导致发货直接失败。
    """
    client = ws_manager.get_client(account_id)
    if client and client.is_connected:
        return True

    logger.info("手动发货前 WS 未连接，尝试自动重启 accountId=%d", account_id)
    try:
        restarted = await ws_manager.restart_account(account_id)
    except Exception:
        logger.error("手动发货前 WS 重启异常 accountId=%d", account_id)
        return False
    if not restarted:
        logger.warning("手动发货前 WS 重启失败（账号不存在或凭据缺失）accountId=%d", account_id)
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        client = ws_manager.get_client(account_id)
        if client and client.is_connected:
            logger.info("手动发货前 WS 自动重连成功 accountId=%d", account_id)
            return True
    logger.warning("手动发货前 WS 自动重连超时 accountId=%d", account_id)
    return False


async def send_delivery_message_result(
    account_id: int,
    s_id: str,
    buyer_user_id: str,
    content: str,
    segments: list[dict[str, Any]] | None = None,
) -> dict:
    """Send once while preserving confirmed/failed/unknown ACK semantics.

    若 segments 非空，优先按 segments 逐条发送（文本/图片），无 segments 时回退单条 content。
    """

    client = ws_manager.get_client(account_id)
    if not client or not client.is_connected:
        if not await _ensure_ws_connected(account_id):
            return {
                "status": "failed",
                "errorCode": "websocket_unavailable",
                "message": "账号消息连接不可用，请等待重连后重试",
                "retrySafe": True,
            }
        client = ws_manager.get_client(account_id)
    if not client._sid:
        return {
            "status": "failed",
            "errorCode": "websocket_not_registered",
            "message": "账号消息连接尚未完成注册，请稍后重试",
            "retrySafe": True,
        }

    cid = s_id if s_id.endswith("@goofish") else f"{s_id}@goofish"
    to_id = (
        buyer_user_id
        if buyer_user_id.endswith("@goofish")
        else f"{buyer_user_id}@goofish"
    )
    # 优先走 segments 多段发送（多条正文+图片），无 segments 时回退单条 content
    if segments:
        normalized_segments = _normalize_segments_local(segments)
        if normalized_segments:
            ok, err = await _execute_segments_delivery(client, cid, to_id, normalized_segments)
            if ok:
                return {"status": "confirmed", "retrySafe": False}
            return {
                "status": "failed",
                "errorCode": "segments_delivery_failed",
                "message": err,
                "retrySafe": True,
            }
    result = await client.send_text_message(cid=cid, to_id=to_id, text=content)
    if not isinstance(result, dict):
        return {
            "status": "unknown",
            "errorCode": "message_ack_unknown",
            "message": "发送结果未确认，请先在闲鱼 App 核对，避免重复发送",
            "retrySafe": False,
        }
    try:
        code = int(result.get("code", 500))
    except (TypeError, ValueError):
        code = 500
    if code == 200:
        return {"status": "confirmed", "retrySafe": False}
    if code == 503 and not result.get("mid") and not result.get("deliveryUnknown"):
        return {
            "status": "failed",
            "errorCode": "websocket_unavailable",
            "message": "账号消息连接不可用，请等待重连后重试",
            "retrySafe": True,
        }
    if result.get("deliveryUnknown") or (code >= 500 and not result.get("mid")):
        return {
            "status": "unknown",
            "errorCode": "message_ack_unknown",
            "message": "发送结果未确认，请先在闲鱼 App 核对，避免重复发送",
            "retrySafe": False,
        }
    return {
        "status": "failed",
        "errorCode": "message_rejected",
        "message": "平台明确拒绝买家消息，请检查会话与账号状态后重试",
        "retrySafe": True,
    }


async def _send_delivery_message(
    account_id: int,
    s_id: str,
    buyer_user_id: str,
    content: str,
) -> bool:
    result = await send_delivery_message_result(account_id, s_id, buyer_user_id, content)
    return result.get("status") == "confirmed"


async def _notify_realtime_delivery_attention(
    db: AsyncSession,
    account_id: int,
    outcome: RealtimeDeliveryOutcome,
) -> None:
    try:
        from .automation_runtime import insert_notification

        message = (
            "实时自动发货需要人工核对。"
            f"本地尝试编号：{outcome.attempt_id}；"
            f"状态：{outcome.status}；"
            f"错误代码：{_safe_log_code(outcome.error_code or 'review_required')}。"
            "请在闲鱼 App 核对消息与订单状态，未知结果不要重复发送。"
        )
        await insert_notification(
            db,
            None,
            "自动发货核对提醒",
            message,
            "自动发货核对提醒",
            "warn",
        )
    except Exception as exc:
        logger.warning(
            "Realtime delivery reminder failed accountId=%d attemptId=%d errorType=%s",
            account_id,
            outcome.attempt_id,
            type(exc).__name__,
        )


def _extract_source_event_id(msg: dict) -> str:
    for key in ("pnmId", "messageId", "msgId", "mid", "eventId"):
        value = str(msg.get(key) or "").strip()
        if value:
            return value
    complete = msg.get("complete_msg") or msg.get("completeMsg")
    if isinstance(complete, str):
        try:
            complete = json.loads(complete)
        except (json.JSONDecodeError, TypeError):
            complete = None
    if isinstance(complete, dict):
        for key in ("pnmId", "messageId", "msgId", "mid", "eventId"):
            value = str(complete.get(key) or "").strip()
            if value:
                return value
    return ""


def _render_delivery_content(
    content: str,
    *,
    buyer_name: str,
    external_order_id: str | None,
) -> str:
    return (
        str(content or "")
        .replace("{buyerUserName}", buyer_name or "买家")
        .replace("{orderId}", external_order_id or "")
        .replace("{goodsTitle}", "")
        .replace("{deliveryTime}", time.strftime("%Y-%m-%d %H:%M:%S"))
    )


def _build_delivery_content(header: str, content: str, footer: str) -> str:
    return "\n".join(
        part
        for part in (
            str(header or "").strip(),
            str(content or "").strip(),
            str(footer or "").strip(),
        )
        if part
    )


def _json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        loaded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _normalize_delivery_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {MODE_CARD, MODE_KAMI}:
        return MODE_CARD
    if normalized == MODE_TEXT:
        return MODE_TEXT
    return normalized or "unconfigured"


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _safe_log_code(value: Any) -> str:
    normalized = _SAFE_LOG_CODE_RE.sub("_", str(value or "").lower()).strip("_")
    return (normalized or "unknown")[:64]


async def _trigger_delivery_for_confirmed_statement(
    db: AsyncSession,
    account_id: int,
    *,
    order_id: Optional[str],
    xy_goods_id: str,
    buyer_user_id: str,
    s_id: str,
    goods_title: str,
    session_id_stmt: int,
) -> None:
    """买家确认发货声明后，触发该订单的发货流程。

    复用 _process_delivery 的状态机逻辑，通过构造虚拟消息触发发货。
    安全保障由 RealtimeDeliveryCoordinator 的 event_key 幂等性保证：
    同一订单的 event_key 相同，不会重复发货。
    """
    logger.info(
        "声明确认后触发发货: accountId=%d orderId=%s xyGoodsId=%s stmtSessionId=%d",
        account_id, order_id, xy_goods_id, session_id_stmt,
    )

    # 构造虚拟消息复用 _process_delivery
    fake_msg = {
        "sId": s_id,
        "senderUserId": buyer_user_id,
        "senderUserName": "",
        "xyGoodsId": xy_goods_id,
        "reminderContent": "买家已确认发货声明",
        "reminderUrl": f"orderId={order_id}&itemId={xy_goods_id}&buyerUserId={buyer_user_id}" if order_id else "",
        "pnmId": f"statement-confirm:{session_id_stmt}",
    }
    await _process_delivery(db, account_id, fake_msg)


# ==================== 商业版同步：数据库层去重保护 ====================


def _normalize_identity(value: Any) -> str:
    return str(value or "").strip().removesuffix("@goofish")


async def _find_duplicate_delivery(
    db: AsyncSession,
    *,
    account_id: int,
    external_order_id: str,
    session_id: str,
    item_id: str,
    peer_id: str,
) -> str:
    """发货去重：命中返回原因字符串，未命中返回空串。

    两级判据：
    1. 有订单号（orderId）→ 直接查 ``realtime_delivery_attempt`` 订单维度记录。
       订单是唯一身份，同买家复购不同订单不会被误判重复。
       - state=success/unknown/message_sent/message_sending/platform_confirming → 跳过
       - state=failed 且 retry_safe=1 → 不拦截（交给状态机重试）
       - state=failed 且 retry_safe=0 → 拦截（配置/内容类失败，需先修配置）
       - state=superseded → 忽略（旧配置时代的作废尝试）
    2. 没有订单号 → 退化为"会话 + 商品 + 买家"10 分钟窗口，避免 WS 周期推送重复发货。
    """

    try:
        if external_order_id:
            row = (
                await db.execute(
                    text(
                        """
                        SELECT id, state, retry_safe, last_error_code,
                               TIMESTAMPDIFF(SECOND, created_time, NOW()) AS age_s
                        FROM realtime_delivery_attempt
                        WHERE account_id = :account_id
                          AND external_order_id = :order_id
                          AND state <> 'superseded'
                        ORDER BY id DESC
                        LIMIT 1
                        """
                    ),
                    {
                        "account_id": int(account_id),
                        "order_id": str(external_order_id),
                    },
                )
            ).mappings().first()
            if row is None:
                return ""
            state = str(row.get("state") or "")
            if state == "failed":
                if int(row.get("retry_safe") or 0) == 1:
                    return ""
                # 配置类失败（规则/商品尚未就绪）在 3 分钟内允许重试：
                # 实测付款消息比商品同步早到，首次触发可能查不到发货规则
                # （delivery_mode_unavailable），1 秒后商品落库就能正常发货。
                if (
                    str(row.get("last_error_code") or "") == "delivery_mode_unavailable"
                    and int(row.get("age_s") or 0) <= 180
                ):
                    return ""
                return "order_attempt_failed_not_retryable"
            return f"order_attempt_state_{state or 'unknown'}"

        if not session_id or not item_id or not peer_id:
            return ""

        sid_variants = {str(session_id).strip(), _normalize_identity(session_id)}
        peer_variants = {str(peer_id).strip(), _normalize_identity(peer_id)}
        row = (
            await db.execute(
                text(
                    """
                    SELECT id, state
                    FROM realtime_delivery_attempt
                    WHERE account_id = :account_id
                      AND item_id = :item_id
                      AND state <> 'superseded'
                      AND (
                        session_id IN :sids
                        OR REPLACE(session_id, '@goofish', '') IN :sids
                      )
                      AND (
                        peer_id IN :peers
                        OR REPLACE(peer_id, '@goofish', '') IN :peers
                      )
                      AND created_time >= DATE_SUB(NOW(), INTERVAL 10 MINUTE)
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).bindparams(
                    bindparam("sids", expanding=True),
                    bindparam("peers", expanding=True),
                ),
                {
                    "account_id": int(account_id),
                    "item_id": str(item_id),
                    "sids": list(sid_variants),
                    "peers": list(peer_variants),
                },
            )
        ).mappings().first()
        if row is None:
            return ""
        return f"session_attempt_state_{row.get('state') or 'unknown'}"
    except Exception as exc:
        logger.warning(
            "发货去重查询失败，降级为不跳过 accountId=%d errorType=%s",
            account_id,
            type(exc).__name__,
        )
        return ""


async def _check_order_already_shipped(
    db: AsyncSession,
    account_id: int,
    external_order_id: str,
) -> bool:
    """第二道防线：检查 xianyu_trade_order.order_status 是否为 3（已发货）。

    即使 delivery_record 无记录，若订单已标记为已发货，也跳过本次发货。
    """
    if not external_order_id:
        return False
    query = text("""
        SELECT order_status
        FROM xianyu_trade_order
        WHERE account_id = :account_id
          AND external_order_id = :order_id
          AND deleted = 0
        LIMIT 1
    """)
    try:
        result = await db.execute(query, {
            "account_id": account_id,
            "order_id": str(external_order_id),
        })
        row = result.fetchone()
        if row and row[0] == 3:
            return True
        return False
    except Exception as exc:
        logger.warning(
            "订单已发货检查失败，降级为不跳过 accountId=%d orderId=%s errorType=%s",
            account_id, external_order_id, type(exc).__name__,
        )
        return False


def _should_skip_payment_trigger(account_id: int, msg: dict) -> bool:
    """付款兜底节流：同一 account_id + pnm_id 在 60 秒内只触发一次。

    防止闲鱼 WS 周期性推送付款消息（同一付款事件不同消息）导致重复触发发货。
    """
    pnm_id = str(msg.get("pnmId") or msg.get("pnm_id") or "").strip()
    if not pnm_id:
        # 没有 pnm_id 时无法节流，允许触发（由其他去重机制防护）
        return False

    throttle_key = f"{account_id}:{pnm_id}"
    now = time.time()
    last_trigger = _payment_trigger_throttle.get(throttle_key)
    if last_trigger and (now - last_trigger) < _PAYMENT_THROTTLE_SECONDS:
        return True

    # 记录本次触发时间
    _payment_trigger_throttle[throttle_key] = now

    # 清理过期条目（超过 5 分钟的），避免字典无限增长
    expired_keys = [
        k for k, v in _payment_trigger_throttle.items()
        if (now - v) > 300
    ]
    for k in expired_keys:
        _payment_trigger_throttle.pop(k, None)

    return False
