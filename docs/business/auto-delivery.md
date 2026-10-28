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

## 6. 端到端到达延迟（2026-10-29 实测与优化）

链路拆解：`平台 → WS 收到 syncPushPackage → 落库 xianyu_chat_message → 规则匹配 → 发卡`。
其中「落库→发卡」实测 0~1 秒，**唯一瓶颈是 `平台→WS 落库`**。

### 6.1 修复前的时间线（订单 5127833412058086516）

| 时间(+07) | 事件 |
|---|---|
| 00:31:46.8 | 平台付款 |
| 00:32:05.4 | WS 才收到这批发货相关消息（**延迟 18.6s**，与"记得及时发货"这条文本消息同批） |
| 00:32:06.5 | 规则命中 ruleId=22 |
| 00:32:07.8 | 卡密发出（attempt 48 success） |

**根因**：`_handle_sync_package` 里"收到回包才发下一轮 ackDiff"的**被动链式同步**。
平台对 `/r/SyncStatus/ackDiff` 是**长轮询**（无数据时把请求挂住 15~30 秒），
且响应包多数不带 `lwp=/r/SyncStatus/ackDiff`，`is_ack_diff` 判定为假 ⇒ 一条连接
（32 秒会话）里只发出 **1 次**同步请求，付款卡片要等下一次同步/下一条文本消息
（攒批）才下发，抖动 0~30 秒。

叠加放大：`_ThreadedWebSocketAdapter.close()` 用 5+5 秒等待，每 32 秒会话回收
实际白丢 ~8 秒入站消息（离线率 ~25%）。

### 6.2 已上线的修复（提交 `c5db4c5` / `a847411`）

| 位置 | 改动 |
|---|---|
| `ws_client.py::_sync_loop` | **新增独立定时器，每 2 秒主动发 ackDiff**，不再依赖回包触发；`_sync_outstanding` 计数，连续 5 个无回包才让位，避免堆积 |
| `ws_client.py::close()` | 关闭等待 5+5s → 1+1s，会话回收空窗 ~9s → ~1.5s |
| `ws_client.py` | `RECONNECT_DELAY` 1 → 0.2s（`WS_RECONNECT_DELAY_SECONDS` 可调） |
| `ws_delivery_handler.py` | 新增 `_process_delivery` 外层按 `accountId+orderId` **asyncio 串行锁**（120s 超时），同订单两条付款消息不再各建 attempt（线上 47 unconfigured + 48 success 的重复） |
| `ws_delivery_handler.py::_find_duplicate_delivery` | `delivery_mode_unavailable` 且 attempt 在 180 秒内 → 视为可重试（商品/规则晚于付款消息落库的竞态，付款 1 秒后才同步到商品时不能永久卡死） |

可调环境变量：`WS_SYNC_POLL_INTERVAL_SECONDS`（默认 2）、`WS_RECONNECT_DELAY_SECONDS`（默认 0.2）。

### 6.3 观测口径

```sql
-- 付款卡片落库延迟（目标 p90 < 2s）
SELECT id, s_id, FROM_UNIXTIME(NULLIF(message_time,0)/1000) mt, created_time,
       TIMESTAMPDIFF(SECOND, FROM_UNIXTIME(NULLIF(message_time,0)/1000), created_time) lag_s
FROM xianyu_chat_message WHERE content_type=26 AND created_time>=DATE_SUB(NOW(),INTERVAL 1 HOUR);
-- 同步节拍（应每 2 秒一条）
journalctl -u xianyu-api --since "2 min ago" -o cat | grep "同步请求已发送" | tail -20
```
