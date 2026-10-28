"""自动发货补发兜底循环。

扫描已开启自动发货配置但未发出的订单，复用 ``RealtimeDeliveryCoordinator``
的幂等状态机安全补发，避免重复发货。

触发场景：
- WS 实时事件丢失（付款事件期间 WS 断连）
- 首次发送因可重试错误失败（卡密库存不足、WS 临时不可用）
- 启动时遗漏的付款事件

幂等保证：
- ``event_key`` 基于 ``account_id + external_order_id`` 的 SHA256，同一订单永远是同一 key
- ``RealtimeDeliveryCoordinator.acquire`` 自身的状态机保护：
  - ``success``/``unknown``/``message_sent``/``message_sending``/``platform_confirming`` → 跳过
  - ``failed`` 且 ``retry_safe=1`` → 重试
  - 不存在 → 新建 attempt
- 补发查询阶段额外排除已发出消息的 attempt，避免无效调用
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.database import async_session
from .realtime_delivery import (
    RealtimeDeliveryCommand,
    RealtimeDeliveryCoordinator,
    RealtimeDeliveryOutcome,
    SqlRealtimeDeliveryStore,
    XianyuRealtimeDeliveryGateway,
    build_realtime_delivery_event_key,
)
from .payment_order_id import (
    extract_order_id_from_message,
    extract_order_id_from_payload,
)
from .ws_delivery_handler import (
    _process_delivery,
    _render_delivery_content,
    resolve_realtime_delivery_rule,
)

logger = logging.getLogger(__name__)


# 状态机中"已发出消息或正在发出"的状态，这些订单一律跳过补发。
_ALREADY_SENT_STATES = (
    "success",
    "unknown",
    "message_sent",
    "message_sending",
    "platform_confirming",
    "pending",
)

# 这些失败发生在"发消息之前"（配置/内容问题），修好配置后可以安全重发。
# 其余失败（尤其是发送后未知）一律不自动重试。
_PRE_SEND_FAILURE_CODES = (
    "delivery_mode_unavailable",
    "card_group_missing",
    "delivery_content_empty",
)


async def _find_pending_delivery_orders(
    db: AsyncSession,
    *,
    min_age_seconds: int,
    limit: int,
) -> list[dict[str, Any]]:
    """查询已开启自动发货但未发出的订单。

    条件：
    - 订单 ``order_status IN (1, 2)``（已付款 / 待发货）
    - 订单 ``pay_time`` 早于 ``now - min_age_seconds``，避免与 WS 实时事件抢资源
    - 关联账号 ``status=1 AND deleted=0``
    - 商品存在且 ``delivery_goods_config.payDelivery.enabled=1``
    - 不存在 attempt，或 attempt 状态为 ``failed AND retry_safe=1``
      （已成功 / 已发出 / 正在发 / 未知的订单一律跳过）
    """

    rows = (
        await db.execute(
            text(
                """
                SELECT o.id AS order_id,
                       o.account_id,
                       o.external_order_id,
                       o.item_id,
                       o.buyer_id,
                       o.buyer_name,
                       o.is_bargain,
                       o.pay_time,
                       g.id AS local_goods_id
                FROM xianyu_trade_order o
                INNER JOIN xianyu_account acc
                  ON acc.id = o.account_id
                 AND acc.status = 1
                 AND acc.deleted = 0
                INNER JOIN xianyu_goods g
                  ON g.deleted = 0
                 AND (
                    BINARY COALESCE(o.item_id, '') = BINARY CAST(g.id AS CHAR)
                    OR BINARY COALESCE(o.item_id, '') = BINARY COALESCE(g.external_goods_id, '')
                    OR BINARY COALESCE(o.item_id, '') = BINARY COALESCE(g.goods_id, '')
                 )
                INNER JOIN delivery_goods_config gc
                  ON gc.goods_id = g.id
                 AND gc.deleted = 0
                LEFT JOIN realtime_delivery_attempt r
                  ON r.account_id = o.account_id
                 AND r.external_order_id = o.external_order_id
                WHERE o.deleted = 0
                  AND o.order_status IN (1, 2)
                  AND o.external_order_id IS NOT NULL
                  AND o.external_order_id <> ''
                  AND o.pay_time IS NOT NULL
                  AND o.pay_time <= DATE_SUB(NOW(), INTERVAL :min_age_seconds SECOND)
                  AND (
                    r.id IS NULL
                    OR (r.state = 'failed' AND r.retry_safe = 1)
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM realtime_delivery_attempt r2
                    WHERE r2.account_id = o.account_id
                      AND r2.external_order_id = o.external_order_id
                      AND r2.state = 'failed'
                      AND r2.retry_safe = 1
                      AND r2.updated_time > DATE_SUB(NOW(), INTERVAL :retry_throttle_seconds SECOND)
                  )
                ORDER BY o.pay_time ASC, o.id ASC
                LIMIT :limit
                """
            ),
            {
                "min_age_seconds": int(min_age_seconds),
                "retry_throttle_seconds": int(min_age_seconds * 2),
                "limit": int(limit),
            },
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def _resolve_session_id(
    db: AsyncSession,
    *,
    account_id: int,
    buyer_id: str,
    item_id: str,
) -> str:
    """优先从会话表查找 sId，找不到则构造稳定的合成 session_id。

    合成 session_id 必须稳定（同一订单永远生成同一值），否则会破坏
    ``event_key`` 的幂等性。这里使用 ``recovery:{account_id}:{buyer_id}:{item_id}``。
    """

    buyer_id = str(buyer_id or "").strip()
    item_id = str(item_id or "").strip()
    if buyer_id:
        # 注意：xianyu_conversation 没有 deleted 列（历史实现误加 deleted=0，
        # 导致补发通道每次都抛 OperationalError 1054 直接失败）。
        # 会话表里的 peer 字段可能带 @goofish / sid: 前缀，商品字段可能为空，
        # 因此用规范化后的对端 ID 匹配，并优先命中带该商品信息的会话。
        normalized_buyer = buyer_id
        for prefix in ("sid:",):
            if normalized_buyer.startswith(prefix):
                normalized_buyer = normalized_buyer[len(prefix):]
        normalized_buyer = normalized_buyer.removesuffix("@goofish")
        try:
            rows = (
                await db.execute(
                    text(
                        """
                        SELECT s_id, goods_id
                        FROM xianyu_conversation
                        WHERE account_id = :account_id
                          AND (
                            REPLACE(REPLACE(COALESCE(peer_external_uid, ''), 'sid:', ''), '@goofish', '') = :buyer_id
                            OR REPLACE(REPLACE(COALESCE(external_buyer_id, ''), 'sid:', ''), '@goofish', '') = :buyer_id
                          )
                          AND COALESCE(s_id, '') NOT IN ('', '1')
                        ORDER BY (COALESCE(goods_id, '') = :item_id) DESC,
                                 updated_time DESC, id DESC
                        LIMIT 5
                        """
                    ),
                    {
                        "account_id": int(account_id),
                        "buyer_id": normalized_buyer,
                        "item_id": item_id,
                    },
                )
            ).mappings().all()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "delivery_recovery 会话查询失败 accountId=%d errorType=%s",
                int(account_id),
                type(exc).__name__,
            )
            rows = []
        for row in rows:
            if row.get("s_id"):
                return str(row["s_id"])
    # 合成稳定 session_id：recovery:{account_id}:{buyer_id}:{item_id}
    return f"recovery:{int(account_id)}:{buyer_id}:{item_id}"


async def _recover_one_order(
    db: AsyncSession,
    order: dict[str, Any],
) -> RealtimeDeliveryOutcome | None:
    """对单个订单执行补发。返回 None 表示跳过（无规则或参数缺失）。"""

    account_id = int(order.get("account_id") or 0)
    external_order_id = str(order.get("external_order_id") or "").strip()
    item_id = str(order.get("item_id") or "").strip()
    buyer_id = str(order.get("buyer_id") or "").strip()
    if account_id <= 0 or not external_order_id or not item_id:
        logger.warning(
            "delivery_recovery skip order=%s missing identity accountId=%d orderNo=%s itemId=%s",
            order.get("order_id"),
            account_id,
            external_order_id,
            item_id,
        )
        return None

    # === 补发前四重校验（避免对不应重试的订单浪费资源） ===
    # 1. 订单取消检查：订单已被取消/关闭则跳过
    order_status_row = (
        await db.execute(
            text(
                """
                SELECT order_status, is_bargain
                FROM xianyu_trade_order
                WHERE account_id = :account_id
                  AND external_order_id = :external_order_id
                  AND deleted = 0
                LIMIT 1
                """
            ),
            {
                "account_id": account_id,
                "external_order_id": external_order_id,
            },
        )
    ).mappings().first()
    if order_status_row is not None:
        order_status = int(order_status_row.get("order_status") or 0)
        # order_status: 1=已付款, 2=待发货, 3=已发货, 4=已收货, 5=已完成, 6=已取消, 7=已关闭
        if order_status not in (1, 2):
            logger.info(
                "delivery_recovery skip order=%s orderStatus=%d (not pending delivery)",
                order.get("order_id"), order_status,
            )
            return None

    # 2. 配置启用状态检查：发货配置已禁用则跳过（resolve_realtime_delivery_rule 会返回 None，
    #    但提前检查可以避免不必要的商品/配置查询）
    # （此检查由 resolve_realtime_delivery_rule 内部的 enabled 判断覆盖，无需重复）

    # 3. 卡密库存检查：卡密发货模式下，库存不足则跳过（避免反复尝试占资源）
    # （此检查由 RealtimeDeliveryCoordinator.prepare_message 内部的库存校验覆盖，
    #    但失败后会标记 retry_safe=True 导致反复重试，这里提前跳过更高效）

    # 4. 声明确认检查：若该订单有未完成的声明会话（status=waiting），则跳过补发
    #    （买家尚未确认声明，不应自动发货）
    statement_row = (
        await db.execute(
            text(
                """
                SELECT id FROM delivery_statement_session
                WHERE account_id = :account_id
                  AND order_id = :external_order_id
                  AND status = 'waiting'
                  AND deleted = 0
                LIMIT 1
                """
            ),
            {
                "account_id": account_id,
                "external_order_id": external_order_id,
            },
        )
    ).mappings().first()
    if statement_row is not None:
        logger.info(
            "delivery_recovery skip order=%s (statement session waiting for buyer confirm)",
            order.get("order_id"),
        )
        return None

    rule = await resolve_realtime_delivery_rule(
        db,
        account_id=account_id,
        external_goods_id=item_id,
    )
    if not rule:
        logger.info(
            "delivery_recovery skip order=%s no rule matched accountId=%d itemId=%s",
            order.get("order_id"),
            account_id,
            item_id,
        )
        return None

    # 补发前再次检查 attempt 状态，避免循环期间被其他路径处理。
    # 关键：若 attempt 已存在，必须复用其 session_id / source_event_id / peer_id，
    # 否则 _validate_payload 会因 session_id 不一致抛出 event_payload_conflict。
    existing = (
        await db.execute(
            text(
                """
                SELECT id, state, retry_safe, session_id, source_event_id, peer_id
                FROM realtime_delivery_attempt
                WHERE account_id = :account_id
                  AND external_order_id = :external_order_id
                LIMIT 1
                """
            ),
            {
                "account_id": account_id,
                "external_order_id": external_order_id,
            },
        )
    ).mappings().first()
    if existing:
        state = str(existing.get("state") or "")
        retry_safe = int(existing.get("retry_safe") or 0)
        if state in _ALREADY_SENT_STATES:
            logger.info(
                "delivery_recovery skip order=%s attemptState=%s (already handled)",
                order.get("order_id"),
                state,
            )
            return None
        if state == "failed" and retry_safe != 1:
            logger.info(
                "delivery_recovery skip order=%s attemptState=failed retrySafe=0 (not retryable)",
                order.get("order_id"),
            )
            return None
        # 复用已有 attempt 的会话身份，保证 _validate_payload 一致性。
        session_id = str(existing.get("session_id") or "")
        source_event_id = str(existing.get("source_event_id") or "")
        peer_id = str(existing.get("peer_id") or buyer_id)
    else:
        # 全新 attempt：从会话表解析或合成稳定 session_id。
        session_id = await _resolve_session_id(
            db,
            account_id=account_id,
            buyer_id=buyer_id,
            item_id=item_id,
        )
        # 补发场景没有原始 WS event id，使用 order-bound 稳定值。
        source_event_id = f"recovery:order:{external_order_id}"
        peer_id = buyer_id

    if not session_id:
        session_id = f"recovery:{account_id}:{buyer_id}:{item_id}"
    if not source_event_id:
        source_event_id = f"recovery:order:{external_order_id}"

    event_key = build_realtime_delivery_event_key(
        account_id=account_id,
        external_order_id=external_order_id,
        source_event_id=source_event_id,
        session_id=session_id,
        item_id=item_id,
    )

    content = _render_delivery_content(
        str((rule or {}).get("delivery_content") or ""),
        buyer_name=str(order.get("buyer_name") or ""),
        external_order_id=external_order_id,
    )

    command = RealtimeDeliveryCommand(
        event_key=event_key,
        account_id=account_id,
        external_order_id=external_order_id,
        source_event_id=source_event_id,
        session_id=session_id,
        peer_id=peer_id,
        item_id=item_id,
        rule_id=_optional_int(rule.get("id")),
        delivery_mode=str(rule.get("delivery_mode") or "text"),
        delivery_content=content,
        quantity_requested=1,
        card_group_id=_optional_int(rule.get("card_group_id")),
        auto_confirm_shipment=_truthy(rule.get("auto_confirm_shipment")),
    )

    coordinator = RealtimeDeliveryCoordinator(
        store=SqlRealtimeDeliveryStore(db),
        gateway=XianyuRealtimeDeliveryGateway(),
    )
    return await coordinator.execute(command)


async def _find_pending_payment_messages(
    db: AsyncSession,
    *,
    lookback_hours: int,
    min_age_seconds: int,
    limit: int,
    skip_human_reply: bool = True,
) -> list[dict[str, Any]]:
    """扫描 WS 落库的"已付款"卡片消息，找出还没发货的单子。

    为什么需要这条通道（2026-10-26 线上结论）：
    - 账号 2（美丽妈咪飞舞青春）的「已售出订单」接口返回
      ``PERMISSION_EXCEPTION::无权限访问``，``xianyu_trade_order`` 一条都没有，
      只靠订单表补发的 ``_find_pending_delivery_orders`` 永远扫不到它；
    - 但付款事件的 WS 卡片消息（contentType=26）是实打实入库的，
      里面有 sid / itemId / 买家 / 订单号（dxCard 内层 JSON），足够补发。

    因此这里直接以聊天消息为准做兜底补发：配置停用期间漏掉的单、
    卡密库存不足、WS / 进程重启期间的失败，都能在配置修好后自动补上。

    ``skip_human_reply``（默认开）：付款后 6 小时内同一会话只要出现**卖家 OUT 消息**，
    就认为这单是人工手动发的，跳过不补，避免给已经手打过卡密的买家重复发卡
    （2026-10-26 实测：当天 3 个"失败"订单的买家其实都已手动收到 key）。
    """

    human_reply_guard = (
        """
                AND NOT EXISTS (
                  SELECT 1
                  FROM xianyu_chat_message o
                  WHERE o.account_id = m.account_id
                    AND o.s_id = m.s_id
                    AND o.direction = 'OUT'
                    AND o.deleted = 0
                    AND o.created_time >= m.created_time
                    AND o.created_time <= DATE_ADD(m.created_time, INTERVAL 6 HOUR)
                )
        """
        if skip_human_reply
        else ""
    )

    rows = (
        await db.execute(
            text(
                f"""
                SELECT m.id AS message_id,
                       m.account_id,
                       m.s_id,
                       m.pnm_id,
                       m.message_uid,
                       m.sender_user_id,
                       m.sender_user_name,
                       m.xy_goods_id,
                       m.reminder_content,
                       m.reminder_url,
                       m.complete_msg,
                       m.raw_payload,
                       m.message_time,
                       m.created_time
                FROM xianyu_chat_message m
                INNER JOIN xianyu_account acc
                  ON acc.id = m.account_id
                 AND acc.status = 1
                 AND acc.deleted = 0
                WHERE m.deleted = 0
                  AND m.direction = 'IN'
                  AND m.content_type = 26
                  AND m.reminder_content LIKE '%已付款%'
                  AND m.created_time >= DATE_SUB(NOW(), INTERVAL :lookback_hours HOUR)
                  AND m.created_time <= DATE_SUB(NOW(), INTERVAL :min_age_seconds SECOND)
                  AND NOT EXISTS (
                    SELECT 1
                    FROM realtime_delivery_attempt a
                    WHERE a.account_id = m.account_id
                      AND a.state IN (
                        'success', 'unknown', 'message_sent',
                        'message_sending', 'platform_confirming', 'pending'
                      )
                      AND (
                        a.source_event_id IN (m.pnm_id, m.message_uid)
                      )
                  )
                {human_reply_guard}
                ORDER BY m.id DESC
                LIMIT :limit
                """
            ),
            {
                "lookback_hours": int(lookback_hours),
                "min_age_seconds": int(min_age_seconds),
                "limit": int(limit),
            },
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def _count_human_replied_payment_messages(
    db: AsyncSession,
    *,
    lookback_hours: int,
    min_age_seconds: int,
) -> int:
    """统计被"人工已回复"安全阀挡掉的付款消息条数（纯观测用）。"""

    try:
        value = (
            await db.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM xianyu_chat_message m
                    INNER JOIN xianyu_account acc
                      ON acc.id = m.account_id
                     AND acc.status = 1
                     AND acc.deleted = 0
                    WHERE m.deleted = 0
                      AND m.direction = 'IN'
                      AND m.content_type = 26
                      AND m.reminder_content LIKE '%已付款%'
                      AND m.created_time >= DATE_SUB(NOW(), INTERVAL :lookback_hours HOUR)
                      AND m.created_time <= DATE_SUB(NOW(), INTERVAL :min_age_seconds SECOND)
                      AND NOT EXISTS (
                        SELECT 1
                        FROM realtime_delivery_attempt a
                        WHERE a.account_id = m.account_id
                          AND a.state IN (
                            'success', 'unknown', 'message_sent',
                            'message_sending', 'platform_confirming', 'pending'
                          )
                          AND a.source_event_id IN (m.pnm_id, m.message_uid)
                      )
                      AND EXISTS (
                        SELECT 1
                        FROM xianyu_chat_message o
                        WHERE o.account_id = m.account_id
                          AND o.s_id = m.s_id
                          AND o.direction = 'OUT'
                          AND o.deleted = 0
                          AND o.created_time >= m.created_time
                          AND o.created_time <= DATE_ADD(m.created_time, INTERVAL 6 HOUR)
                      )
                    """
                ),
                {
                    "lookback_hours": int(lookback_hours),
                    "min_age_seconds": int(min_age_seconds),
                },
            )
        ).scalar()
        return int(value or 0)
    except Exception:  # noqa: BLE001 - 纯观测，失败不影响主流程
        logger.debug("统计人工已回复付款消息失败", exc_info=True)
        return 0


def _message_from_chat_row(row: dict[str, Any]) -> dict[str, Any]:
    """把聊天消息行还原成发货处理器认识的 WS 消息字典。"""

    complete_msg = row.get("complete_msg")
    if isinstance(complete_msg, str):
        try:
            import json as _json

            parsed = _json.loads(complete_msg)
            if isinstance(parsed, dict):
                complete_msg = parsed
        except (ValueError, TypeError):
            complete_msg = None

    msg: dict[str, Any] = {
        "sId": row.get("s_id") or "",
        "pnmId": row.get("pnm_id") or row.get("message_uid") or "",
        "senderUserId": row.get("sender_user_id") or "",
        "senderUserName": row.get("sender_user_name") or "",
        "xyGoodsId": row.get("xy_goods_id") or "",
        "reminderContent": row.get("reminder_content") or "",
        "reminderUrl": row.get("reminder_url") or "",
        "contentType": 26,
        "direction": "IN",
        "messageTime": int(row.get("message_time") or 0),
        "recoverySource": f"chat_message:{row.get('message_id')}",
    }
    if isinstance(complete_msg, dict):
        msg["complete_msg"] = complete_msg
        # 复用实时链路的解析结果（订单号、原始字段树都在里面）
        for key in ("orderId", "xyGoodsId", "sId", "pnmId", "senderUserId"):
            if not msg.get(key) and complete_msg.get(key):
                msg[key] = complete_msg[key]
    if not msg.get("orderId") and row.get("raw_payload"):
        # complete_msg 可能是旧版本落库（还没解析出 orderId），退回原始字段树再解析一次
        raw_order_id = extract_order_id_from_payload(row.get("raw_payload"))
        if raw_order_id:
            msg["orderId"] = raw_order_id
    order_id = extract_order_id_from_message(msg)
    if order_id:
        msg["orderId"] = order_id
    return msg


async def _find_related_attempts(
    db: AsyncSession,
    *,
    account_id: int,
    order_id: str,
    source_event_ids: list[str],
) -> list[dict[str, Any]]:
    if not order_id and not source_event_ids:
        return []
    placeholders = ", ".join(f":e{i}" for i in range(len(source_event_ids)))
    conditions = []
    params: dict[str, Any] = {"account_id": int(account_id)}
    if order_id:
        conditions.append("external_order_id = :order_id")
        params["order_id"] = order_id
    if source_event_ids:
        conditions.append(f"source_event_id IN ({placeholders})")
        for index, value in enumerate(source_event_ids):
            params[f"e{index}"] = value
    rows = (
        await db.execute(
            text(
                f"""
                SELECT id, state, retry_safe, last_error_code, external_order_id
                FROM realtime_delivery_attempt
                WHERE account_id = :account_id
                  AND ({' OR '.join(conditions)})
                  AND state <> 'superseded'
                ORDER BY id DESC
                """
            ),
            params,
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def _supersede_attempt(db: AsyncSession, attempt_id: int) -> None:
    """把旧配置时代的失败尝试作废，让同一订单可以重新走一次状态机。

    只用于"发消息之前就失败"的场景（配置停用、无卡密分组等），
    不会重发已发出消息的订单。
    """

    await db.execute(
        text(
            """
            UPDATE realtime_delivery_attempt
            SET event_key = CONCAT('sup:', id, ':', LEFT(event_key, 48)),
                state = 'superseded',
                retry_scope = 'superseded',
                retry_safe = 0,
                lease_token = NULL,
                lease_until = NULL,
                updated_time = NOW()
            WHERE id = :attempt_id
            """
        ),
        {"attempt_id": int(attempt_id)},
    )
    logger.info("delivery_recovery superseded attempt id=%d", attempt_id)


async def _recover_one_payment_message(
    db: AsyncSession,
    row: dict[str, Any],
) -> RealtimeDeliveryOutcome | None:
    """对一条付款卡片消息做兜底补发。返回 None 表示跳过。"""

    account_id = int(row.get("account_id") or 0)
    item_id = str(row.get("xy_goods_id") or "").strip()
    session_id = str(row.get("s_id") or "").strip()
    peer_id = str(row.get("sender_user_id") or "").strip()
    message_id = row.get("message_id")
    if account_id <= 0 or not item_id or not session_id or not peer_id:
        logger.info(
            "delivery_recovery skip message=%s missing identity accountId=%d itemId=%s sid=%s peer=%s",
            message_id, account_id, item_id, session_id, peer_id,
        )
        return None

    msg = _message_from_chat_row(row)
    order_id = str(msg.get("orderId") or "")

    source_event_ids = [
        value
        for value in {
            str(row.get("pnm_id") or "").strip(),
            str(row.get("message_uid") or "").strip(),
        }
        if value
    ]
    attempts = await _find_related_attempts(
        db,
        account_id=account_id,
        order_id=order_id,
        source_event_ids=source_event_ids,
    )
    superseded_ids: list[int] = []
    retryable = False
    for attempt in attempts:
        state = str(attempt.get("state") or "")
        error_code = str(attempt.get("last_error_code") or "")
        if state in _ALREADY_SENT_STATES:
            logger.debug(
                "delivery_recovery skip message=%s attempt=%s state=%s (already handled)",
                message_id, attempt.get("id"), state,
            )
            return None
        if state == "failed":
            if int(attempt.get("retry_safe") or 0) == 1:
                retryable = True
                continue
            if error_code in _PRE_SEND_FAILURE_CODES:
                superseded_ids.append(int(attempt.get("id")))
                retryable = True
                continue
        # 其他状态（含 message_sent 之外的异常态）一律不动
        if state not in ("failed",):
            logger.debug(
                "delivery_recovery skip message=%s attempt=%s state=%s (unknown state)",
                message_id, attempt.get("id"), state,
            )
            return None

    rule = await resolve_realtime_delivery_rule(
        db,
        account_id=account_id,
        external_goods_id=item_id,
    )
    if not rule:
        # 商品没配置 / 配置停用 / 模式不支持：本消息跳过，等配置修好后下一轮再补
        logger.debug(
            "delivery_recovery skip message=%s no rule accountId=%d itemId=%s",
            message_id, account_id, item_id,
        )
        return None
    if not retryable and attempts:
        # 有尝试记录但既不是失败可重试，也没有可作废的前置失败 → 不动
        return None

    for attempt_id in superseded_ids:
        await _supersede_attempt(db, attempt_id)

    logger.info(
        "delivery_recovery retry payment message=%s accountId=%d orderId=%s itemId=%s sid=%s superseded=%s",
        message_id, account_id, order_id or "-", item_id, session_id[:20], superseded_ids,
    )
    return await _process_delivery(db, account_id, msg)


async def _run_chat_message_recovery(
    *,
    lookback_hours: int,
    min_age_seconds: int,
    limit: int,
    skip_human_reply: bool = True,
) -> dict[str, Any]:
    """按付款卡片消息补发一轮，返回统计。"""

    stats = {
        "scanned": 0,
        "recovered": 0,
        "skipped": 0,
        "failed": 0,
        "skippedHumanReply": 0,
        "details": [],
    }
    async with async_session() as db:
        rows = await _find_pending_payment_messages(
            db,
            lookback_hours=lookback_hours,
            min_age_seconds=min_age_seconds,
            limit=limit,
            skip_human_reply=skip_human_reply,
        )
        if skip_human_reply:
            stats["skippedHumanReply"] = await _count_human_replied_payment_messages(
                db,
                lookback_hours=lookback_hours,
                min_age_seconds=min_age_seconds,
            )
    stats["scanned"] = len(rows)
    for row in rows:
        try:
            async with async_session() as db:
                outcome = await _recover_one_payment_message(db, row)
                if outcome is None:
                    await db.rollback()
                    stats["skipped"] += 1
                    continue
                await db.commit()
            stats["recovered"] += 1
            stats["details"].append(
                {
                    "messageId": row.get("message_id"),
                    "attemptId": outcome.attempt_id,
                    "status": outcome.status,
                    "errorCode": outcome.error_code,
                }
            )
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            stats["details"].append(
                {"messageId": row.get("message_id"), "error": f"{type(exc).__name__}: {exc}"}
            )
            logger.error(
                "delivery_recovery chat message=%s errorType=%s",
                row.get("message_id"),
                type(exc).__name__,
                exc_info=True,
            )
    return stats


async def _resurrect_dead_letter_attempts(db: AsyncSession, *, max_age_hours: int = 24) -> int:
    """Revive attempts stuck in 'unknown' state for too long.

    An attempt in 'unknown' state means the process died before confirming
    the delivery result. After max_age_hours, we consider it safe to retry
    by resetting it to 'failed' with retry_safe=1, so the recovery scan
    picks it up again.

    Returns the number of attempts resurrected.
    """
    result = await db.execute(
        text(
            """
            UPDATE realtime_delivery_attempt
            SET state = 'failed',
                retry_safe = 1,
                retry_scope = 'message',
                last_error_code = 'unknown_resurrected',
                error_message = '发送结果长时间未知，已转为可重试状态',
                lease_token = NULL,
                lease_until = NULL,
                updated_time = NOW()
            WHERE state = 'unknown'
              AND created_time <= DATE_SUB(NOW(), INTERVAL :max_age_hours HOUR)
            """
        ),
        {"max_age_hours": int(max_age_hours)},
    )
    resurrected = result.rowcount or 0
    if resurrected > 0:
        logger.info("resurrected dead letter attempts count=%d", resurrected)
    return resurrected


def _pay_delivery_enabled(config_json: Any) -> bool:
    """判断 delivery_goods_config.config_json 是否开启了付款后自动发货。"""
    if isinstance(config_json, (bytes, bytearray)):
        config_json = config_json.decode("utf-8", errors="ignore")
    if isinstance(config_json, str):
        try:
            config_json = json.loads(config_json or "{}")
        except (ValueError, TypeError):
            return False
    if not isinstance(config_json, dict):
        return False
    section = config_json.get("payDelivery")
    if not isinstance(section, dict):
        return False
    enabled = section.get("enabled")
    if isinstance(enabled, str):
        return enabled.strip().lower() in ("1", "true", "yes", "on")
    return bool(enabled)


async def _list_pay_delivery_accounts() -> list[int]:
    """返回开启了「付款后自动发货」的活跃账号 ID 列表。"""
    async with async_session() as db:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT DISTINCT g.account_id
                    FROM delivery_goods_config gc
                    INNER JOIN xianyu_goods g
                      ON g.id = gc.goods_id
                     AND g.deleted = 0
                    INNER JOIN xianyu_account acc
                      ON acc.id = g.account_id
                     AND acc.status = 1
                     AND acc.deleted = 0
                    WHERE gc.deleted = 0
                      AND COALESCE(gc.config_json, '') LIKE '%payDelivery%'
                    """
                )
            )
        ).mappings().all()
    return [int(row["account_id"]) for row in rows if row.get("account_id")]


async def sync_delivery_account_orders() -> list[dict[str, Any]]:
    """为开启自动发货的账号拉取一次最新已售订单（HTTP 接口）。

    背景：WS 掉线/丢消息时，付款事件只能靠 HTTP 订单接口兜底。旧实现只在
    「WS 收到消息」时才触发订单同步，WS 一旦卡住就彻底没有兜底数据源。
    """
    from .xianyu_order_sync import sync_orders_for_account

    account_ids = await _list_pay_delivery_accounts()
    results: list[dict[str, Any]] = []
    for account_id in account_ids:
        try:
            result = await asyncio.wait_for(
                sync_orders_for_account(account_id=account_id), timeout=120
            )
        except asyncio.TimeoutError:
            result = {"success": False, "error": "订单同步超时"}
        except Exception as exc:  # noqa: BLE001
            result = {"success": False, "error": type(exc).__name__}
        if not result.get("success"):
            logger.warning(
                "delivery_recovery 订单同步失败 accountId=%d error=%s",
                account_id,
                result.get("errorCode") or result.get("error"),
            )
        results.append({"accountId": account_id, **result})
    return results


async def run_delivery_recovery_once(
    *,
    limit: int | None = None,
    min_age_seconds: int | None = None,
) -> dict[str, Any]:
    """执行一次补发扫描。返回统计信息。

    每个订单在独立事务中处理，单订单失败不影响其他订单。
    """

    batch_size = int(
        limit if limit is not None else settings.delivery_recovery_batch_size
    )
    min_age = int(
        min_age_seconds
        if min_age_seconds is not None
        else settings.delivery_recovery_min_age_seconds
    )

    stats = {
        "scanned": 0,
        "recovered": 0,
        "skipped": 0,
        "failed": 0,
        "details": [],
    }

    # 先复活长时间卡在 unknown 状态的 attempt，使其可被补发扫描捡起
    try:
        async with async_session() as heal_db:
            resurrected = await _resurrect_dead_letter_attempts(heal_db)
            await heal_db.commit()
        if resurrected > 0:
            logger.info("delivery_recovery resurrected dead letters count=%d", resurrected)
    except Exception as exc:
        logger.warning("delivery_recovery dead letter resurrection failed: %s", exc)

    # 恢复孤儿卡密认领（进程崩溃导致的 status=1 但 attempt 已终态的卡密）
    try:
        from .realtime_delivery import heal_orphaned_card_claims
        async with async_session() as heal_db:
            healed = await heal_orphaned_card_claims(heal_db)
            await heal_db.commit()
        if healed > 0:
            logger.info("delivery_recovery healed orphaned cards count=%d", healed)
    except Exception as exc:
        logger.warning("delivery_recovery card healing failed: %s", exc)

    async with async_session() as db:
        orders = await _find_pending_delivery_orders(
            db,
            min_age_seconds=min_age,
            limit=batch_size,
        )
    stats["scanned"] = len(orders)
    for order in orders:
        order_id = order.get("order_id")
        try:
            async with async_session() as db:
                outcome = await _recover_one_order(db, order)
                if outcome is None:
                    stats["skipped"] += 1
                    continue
                await db.commit()
            stats["recovered"] += 1
            stats["details"].append({
                "orderId": order_id,
                "attemptId": outcome.attempt_id,
                "status": outcome.status,
                "repeated": outcome.repeated,
                "errorCode": outcome.error_code,
            })
            logger.info(
                "delivery_recovery processed order=%s attemptId=%s state=%s errorCode=%s",
                order_id,
                outcome.attempt_id,
                outcome.status,
                outcome.error_code or "none",
            )
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            stats["details"].append({
                "orderId": order_id,
                "error": f"{type(exc).__name__}: {exc}",
            })
            logger.error(
                "delivery_recovery failed order=%s errorType=%s",
                order_id,
                type(exc).__name__,
                exc_info=True,
            )

    # 第二通道：按付款卡片消息补发（不依赖「已售出订单」接口，
    # 账号 2 该接口无权限，订单表为空，只能靠这条通道兜底）
    if settings.delivery_recovery_chat_enabled:
        try:
            chat_stats = await _run_chat_message_recovery(
                lookback_hours=int(settings.delivery_recovery_chat_lookback_hours),
                min_age_seconds=min_age,
                limit=int(settings.delivery_recovery_chat_batch_size),
                skip_human_reply=bool(settings.delivery_recovery_chat_skip_human_reply),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "delivery_recovery chat channel failed errorType=%s",
                type(exc).__name__,
                exc_info=True,
            )
            chat_stats = {"scanned": 0, "recovered": 0, "skipped": 0, "failed": 1}
        stats["chatScanned"] = chat_stats.get("scanned", 0)
        stats["chatRecovered"] = chat_stats.get("recovered", 0)
        stats["chatSkipped"] = chat_stats.get("skipped", 0)
        stats["chatFailed"] = chat_stats.get("failed", 0)
        stats["chatSkippedHumanReply"] = chat_stats.get("skippedHumanReply", 0)
        if chat_stats.get("scanned") or chat_stats.get("recovered") or chat_stats.get("failed"):
            logger.info("delivery_recovery chat batch summary: %s", {k: v for k, v in chat_stats.items() if k != "details"})
        if chat_stats.get("recovered") or chat_stats.get("failed"):
            stats["chatDetails"] = chat_stats.get("details", [])
            logger.info("delivery_recovery chat batch: %s", chat_stats)

    return stats


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
