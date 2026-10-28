# 自动发货机制（含"人工干预是否阻止发货"结论）

> 调查日期：2026-10-28；代码基线 `0ec9156`（与线上服务器 C `/var/www/xianyu` 一致）

## 1. 三个入口

| 通道 | 代码 | 触发条件 | 是否看"人工干预/人工说话" |
|---|---|---|---|
| 实时自动发货 | `app/services/ws_delivery_handler.py::_process_delivery`（outbox `delivery` 分支，见 `ws_startup.py:337`） | WS 落库消息 `is_payment_message()` 为真（contentType=26 且提醒文本含"已付款/等待发货"，显式排除"待付款"） | **否，完全不看** |
| 兜底补发①（订单） | `delivery_recovery.py::_find_pending_delivery_orders` | `xianyu_trade_order.order_status IN (1,2)` 且 `pay_time` 已过 `min_age`、无 attempt 或 `failed & retry_safe=1` | **否** |
| 兜底补发②（聊天消息） | `delivery_recovery.py::_find_pending_payment_messages` | 付款卡片消息（IN/contentType=26/含"已付款"）无成功 attempt | **是**：`skip_human_reply=1`（默认，`DELIVERY_RECOVERY_CHAT_SKIP_HUMAN_REPLY`）时，付款后 6h 内同会话存在任意 `direction='OUT'` 消息即跳过 |

## 2. 实时通道的唯一门禁（按顺序）

1. `is_payment_message()` 关键词判定（排除"拍下待付款"）；
2. 60 秒节流：`account_id + pnmId` 相同不重复触发（`_PAYMENT_THROTTLE_SECONDS`）；
3. 上下文完整性：`sId / itemId / senderUserId / sourceEventId` 缺一即拒绝；
4. 去重：有订单号 → 查 `realtime_delivery_attempt`（订单维度）；无订单号 → 会话+商品+买家 **10 分钟** 窗口；
5. `_check_order_already_shipped()`：本地 `xianyu_trade_order.order_status=3` 则跳过；
6. 规则匹配 `resolve_realtime_delivery_rule()`：`delivery_goods_config.payDelivery.enabled=1` 且 mode ∈ {text, card}，否则 `delivery_mode_unavailable` 等失败码；
7. SKU 覆盖、发货声明（`should_send_statement`）分支；
8. 发消息 → 写 `realtime_delivery_attempt` + `delivery_record`。

**没有任何对 `xianyu_conversation.auto_reply_paused`、`last_manual_reply_at`、卖家 OUT 消息的检查。**

## 3. "人工干预"机制只作用于 AI 自动回复

- `ws_startup.py::_pause_auto_reply_for_manual_outbound`：卖家 OUT 且 `is_auto_reply=0` → `auto_reply_paused=1` + `last_manual_reply_at`。
- `automation_runtime.py::_check_auto_reply_scope`：暂停后 **60 秒**无新人工回复自动恢复；买家发"开启自动回复"可恢复。
- `ai_auto_reply_policy.py`：预发送策略再查一次"近 `humanInterventionPauseMinutes`（默认 30 分钟）内是否有非 AI 的 OUT 消息"，命中则 `human_intervention_active`。
- 这三处都不影响发货。人工模式（`mode=manual`）同样只停 AI 回复。

## 4. 线上实证（服务器 C，2026-10-26 19:31-19:32）

会话 `68535169526@goofish`：

| 时间 | 事件 |
|---|---|
| 19:31:32 | 买家问"是中转站吗"，**卖家人工回复"对，官方中转"**（无 `ai_auto_reply_attempt` 记录） |
| 19:32:37 | 买家"我已付款，等待你发货" |
| 19:32:39 | 系统自动发出卡密（`realtime_delivery_attempt.id=40` state=success，orderId=3316544870142002056） |

→ **人工刚说过话，自动发货照常执行**。同类样本：attempt 25/29/30/40 在付款前 6h 内均有卖家 OUT 消息，仍全部 success。

## 5. 已知缺口 / 风险

1. **人工已在闲鱼 App 手打钥匙 → 实时通道仍可能重复发**（只要付款事件重放/兜底订单通道命中）。兜底聊天通道有 6h 安全阀，兜底订单通道没有。
2. **聊天通道安全阀会误挡**：它只判 `direction='OUT'`，把 AI 回复、以及系统自己发出的发货消息都算作"人工已回复"。付款后 6h 内只要会话里出现过任何 OUT 消息，该单就永久跳过补发。
3. **无订单号的付款事件**（线上 attempt 25/30 的 `external_order_id` 仍为 NULL）只能靠 10 分钟会话窗口去重，重复风险更高。
4. **`auto_reply_paused` 在生产从未落库**：库内 227 个会话 `auto_reply_paused=1` 为 0 条、`last_manual_reply_at` 全为 NULL。即 `_pause_auto_reply_for_manual_outbound` 实际未生效（疑似 `msg['direction']` 未赋值，或 `s_id` 带 `@goofish` 后缀 UPDATE 不中），真正拦住 AI 的只是 policy 层直接查 `xianyu_chat_message` 的兜底判断（14 天内 1 条 `human_intervention_active`）。待修。
