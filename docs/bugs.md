# 问题与踩坑记录（docs/bugs.md）

## 2026-10-06：商品已开启自动回复且 AI 测试正常，但买家消息没有 AI 回复

**现象**

- 商品 `1085707255590`、`1086593466124` 的 `auto_reply_enabled=1`。
- 全局 AI 客服配置 `enabled=true`、`mode=hybrid`，模型测试接口可正常返回。
- 第一个商品有真实会话和买家消息，但 `ai_auto_reply_attempt` 总数为 0，说明从未进入模型调用。
- 第二个商品线上没有任何 `xianyu_conversation` 或消息记录，无法完成真实链路验证。

**根因**

消息在模型调用前被运行策略拦截，共两处：

1. `_is_late_incoming_before_latest_outbound`
   - 买家消息时间早于同一会话较新的卖家 `OUT` 消息，且相差不超过 5 分钟。
   - 该批次被视为迟到/重放消息，避免在人工已经回复后再次外发。
2. `evaluate_ai_auto_reply_policy`
   - 当前配置 `pauseOnHumanIntervene=true`、`humanInterventionPauseMinutes=30`。
   - 最近 30 分钟内只要有非 AI 的卖家 `OUT` 消息，就返回
     `human_intervention_active`，跳过模型调用。

会话状态机只暂停约 60 秒，但运行时策略仍拦截 30 分钟，导致页面上的会话状态看起来已恢复，
实际自动回复仍被拒绝。AI 配置页的“测试 AI 回复”只调用模型，不经过作用域、迟到保护、
人工接管和发送链路，因此测试成功不能证明自动回复已端到端生效。

**线上证据**

- `user_business_setting.ai-customer-service` 于 `2026-10-06 17:21:58` 保存为
  `enabled=true`、`hybrid`、`pauseOnHumanIntervene=true`、
  `humanInterventionPauseMinutes=30`。
- 六个历史只读复现消息的 AI 分支均为 `completed` 且无错误码，但
  `ai_auto_reply_attempt` 为空。
- 会话 `67432631607@goofish` 在 16:07-16:09 连续存在买家和卖家人工交替消息；
  买家消息均处于人工接管窗口，且部分消息早于最新卖家回复不超过 5 分钟。
- 线上 `ai_auto_reply_attempt` 总数为 0，排除“模型调用失败后留下错误记录”的情况。

**恢复与验证**

- 临时验证：确认最近 30 分钟没有人工 `OUT` 后，让买家在目标商品发送一条新消息。
- 如需人工回复后快速恢复 AI：关闭 `pauseOnHumanIntervene`，或把
  `humanInterventionPauseMinutes` 调整为 1。
- 第二个商品需要产生真实会话后再验证完整链路，仅开启作用域不足以证明可用。

**待修复**

- 统一会话状态机的 60 秒恢复逻辑与运行时策略的 30 分钟人工接管窗口。
- 在自动回复日志/outbox 中直接记录 `human_intervention_active`、
  `late_incoming_suppressed` 等跳过原因，便于运营查看。

## 2026-10-06：WebSocket 同步包字段错位和空会话标识重复记录

**现象**

- 日志持续出现：
  - `文本内容错位到会话标识字段，已修复`
  - `会话标识疑似字段错位，已清空`
  - `WS 消息字段不完整`
- `xianyu_chat_message` 中存在 `s_id`、发送者、商品均为空且
  `parse_status=failed` 的 `IN` 记录，内容是先前卖家人工 `OUT` 消息的回放。
- WebSocket 约每 40 秒出现一次 `WebSocketConnectionClosedException` 后重连。

**影响**

- 污染消息表、重复发送通知并放大日志量。
- 某些新版同步包格式可能无法正确映射会话和发送者，存在漏处理真实买家消息的风险。

**根因**

- `decrypt_payload()` 使用递归方式收集 MessagePack 解包结果中的全部字典。
- 一个完整消息的 `content` 等嵌套字典也被当成独立消息返回，随后解析成没有
  `sId/pnmId/senderUserId` 的伪消息。
- 同一真实消息因此形成“正常消息 + 空会话失败消息”两条记录，并持续进入回放/断线重连链路。
- 服务器 C 为 `Asia/Ho_Chi_Minh (+07)`，北京时间比服务器快 1 小时；AI 策略使用
  `Asia/Shanghai` 且工作时段为 24 小时。北京时间 18:48 对应服务器 17:48，
  时区不会触发工作时段拦截。

**修复与验证**

- `_collect_dicts()` 改为 `_collect_payload_objects()`，每个 MessagePack 顶层对象只收集顶层
  message container，不再递归普通 `dict` 的子节点；仍支持 list 和 JSON 字符串中的对象。
- 新增 `test_ws_protocol_nested_payload.py`，覆盖嵌套 `content` 只生成一条消息。
- API 测试结果：`8 passed`；`compileall` 和 `git diff --check` 通过。
- commit `8b382de` 已推送至 `gaoshou18080-afk/xianyu-pilot` fork；服务器 C 已快进更新并重启 API。
- 重启后观察 95 秒，旧四类字段错位告警为 `0`；新消息 `id=188` 正常落库且未生成配对伪消息。
- 后续仍需观察 WebSocket 周期性断线和无法恢复会话身份数据包的补偿策略。

## 2026-10-26：卡密自动发货"已启用"但付款后不发货（配置被保存动作写成停用）

**现象**

- 后台自动发货页显示商品 `1091821311650` 已启用卡密发货，但 09:23:14 的付款消息
  （`xianyu_chat_message.id=869`）没有自动发货，`realtime_delivery_attempt.id=27` 落成
  `state=failed / last_error_code=delivery_mode_unavailable`，错误文案"API/custom 发货模式已停用"，
  日志为 `Realtime delivery no rule matched accountId=2 itemId=1091821311650`。
- 同一商品 09:17、09:20 两笔都成功（ruleId=8，card，cardGroupId=4），说明链路本身可用。

**根因（binlog 实证）**

- `binlog.000165`：09:21:16（goods 17，pos 14891682）与 09:21:23（goods 25，pos 14945081）两次
  `PUT /api/auto-delivery/goods/{id}/config` 把 `payDelivery.enabled` 从 `1` 写成 `0`，
  JSON 其余字段（mode=card、cardGroupId=4、cardTemplate/header/content/footer 等）逐字段比对**完全一致**。
- 即：保存请求把整份表单（含 `enabled=0`）提交上去，后端 `save_goods_delivery_config` 只做字段合并，
  不存在后台改写逻辑。前端 `fillConfigForm` 默认 `enabled=1`，`_config` 读取路径也只读不转换，
  最可能是弹窗内"启用/停用"下拉被误改（滚轮/键盘在 select 上滚动即可静默切换）。
- 规则未命中时实时链路直接落 failed，不发任何提醒，用户端表现为"配置是开的但不发货"。

**排查要点**

- `delivery_goods_config.config_json` 里是 `{"payDelivery":{"enabled":...}}`，不是平铺的 `enabled` 字段，
  用 `JSON_EXTRACT(config_json,'$.payDelivery.enabled')` 查。
- `xianyu_goods`（不是 `goods`）按 `account_id + external_goods_id` 映射到本地 goods.id，
  配置再按 goods.id 命中；商品缺失时会用订单项补建占位商品（曾产生 account 1 的重复占位 goods 28）。
- 付款事件是否进入发货：看 `realtime_delivery_attempt`，没有新行 = WS 未判定为付款事件或消息未入库。

**验证**

- 只读脚本 `resolve_realtime_delivery_rule(account_id=2, external_goods_id=...)` 复算：
  `1091821311650` → ruleId=8、`1094823097374` → ruleId=7，均 card / cardGroupId=4 / 模版 133 字；
  `1094895404770` → 无规则（goods 16 从未保存过发货配置）。
- 09:49:43/09:49:50 用户重新启用后，截至 10:30 无新付款事件，尚无真实下单复核。

**建议**

- 保存配置接口增加审计日志（goods_id、旧 `enabled`、新 `enabled`、timing），便于发现误操作。
- 前端"启用"改为 checkbox 并加防误触，列表页显式提示"当前为停用"。
- 无规则命中时给出前台提醒（通知/站内消息），避免静默失败。

## 2026-10-26：自动发货"漏识别订单"的机制性缺口（实时通知是唯一入口 + 兜底对账失效）

**机制**：付款 → WS 收到 contentType=26 的 `[我已付款，等待你发货]` 通知 → 关键词判定
（`is_payment_message`）→ 去重（sid+item+buyer，成功 10 分钟 / 失败 1 小时）→ 命中
`delivery_goods_config.payDelivery.enabled=1` → 取卡密 → 发消息 → 写 `delivery_record`
+ `realtime_delivery_attempt`。**没有轮询对账**，通知没到就没有任何记录。

**实测（2026-10-26）**

- 付款通知 12 条 ↔ `realtime_delivery_attempt` 12 条（8 成功 / 4 失败），1:1，说明"入库的通知"没漏。
- 但会话 `68378499609` 10:52 卖家手工发密钥、有 `[我已发货]` 通知却**没有任何付款通知和 attempt** → 实证漏单。
- `xianyu_trade_order`：account 1 有 279 条（与平台 `totalCount=279` 一致，今天仅 1 单）；
  account 2 **一条订单都没有**，`fetch_sold_orders_page(2)` 返回 `PERMISSION_EXCEPTION::无权限访问`。
- `delivery_recovery` 依赖 `xianyu_trade_order.pay_time` 扫描，account 2 无订单 → 兜底永远扫不到。
- 订单号丢失：4 条失败日志均 `orderId=None`，`delivery_record.order_id` 全为 NULL
  （付款通知的 orderId 藏在 dxCard 内层 JSON 的 `idleDeliver?orderId=` 里，`field10.reminderUrl` 拿不到）。
- WS 被服务端每 ~40 秒断开一次，2026-10-26 当天 `接收循环连接关闭` 3034 次。
- 应用 `logger.info` 无输出（无 logging 配置，root 默认 WARNING），漏单无日志可查。

**衍生风险**

- 去重键不含订单号：同一买家 10 分钟内重复买同一商品会被静默跳过。
- 若 account 2 订单同步修好而订单号仍为 NULL，`delivery_recovery` 的
  `r.external_order_id = o.external_order_id` 关联不上 → 已发货订单会被重复发卡。

## 2026-10-26：WebSocket 每 40 秒固定断线（平台侧 35 秒会话上限）

**现象**

- `journalctl -u xianyu-api` 里 `WS 底层错误 ... WebSocketConnectionClosedException` +
  `WS 接收循环连接关闭` 全天 3175 次，每账号稳定 **41 秒一轮**（40/41 秒差值占 96%）。
- 断线无 close 帧（`code=None`，TCP 直接被丢），客户端 `_on_error` 收到
  `Connection to remote host was lost.`。

**实证（服务器 C 只读探针，脚本 `/tmp/ws_probe_*.py`，同一账号 2）**

复刻 `ws_client` 连接流程（`get_ws_token` → WSS → `/reg` → `/r/SyncStatus/ackDiff` →
心跳），6 组对照，**每次都在连接建立后 36.3±0.2 秒被断**：

| 实验 | 客户端行为 | 存活 |
|---|---|---|
| A | 只发 15s 心跳，不发 ackDiff | 36.3s |
| B | 收到服务端 `/!` 回 pong（带 mid） | 36.3s |
| C | 每 5s 持续 ackDiff 轮询 + 回 pong | 36.3s |
| E | 强制刷新 mtop token 后再连 | 36.3s |
| F | `/!` 回带 `code=200` 的 pong | 36.5s |
| D | 每 20s 重新 `/reg` | **24.3s（re-reg 立即被杀）** |

结论：**不是心跳写错、不是 token 过期、不是 ackDiff 没轮询**，是平台对该
`did/` 会话的下发上限 ~35 秒（服务端在 +30s 发一个 `/!`，5 秒后丢连接），
客户端参数无法延长。附带确认：**不能周期性重新 `/reg`**，会立刻断线。

**证据链（重连补偿缺失）**

- 每次重连服务端在 +3s 下发一个 `/s/vulcan`，622KB、200 条，全部
  `bizType=370 / bizType="xbroadcast"`（搜索/营销广播，JSON），**不含聊天消息**，
  即重连后**没有聊天消息补发**；任何在 5~6 秒重连窗口内到达的付款通知永久丢失。
- 因此每账号每天约 1587 次重连 × 6 秒 ≈ 2.6 小时（11~14%）处于断线，
  付款消息按比例丢失（今日实证漏单：会话 `68378499609` 无付款通知、无 attempt）。

**日志噪声（同源）**

- `WS 未处理的消息类型 topLevelFieldCount=2` 128537 条/天：其实是心跳的裸 ACK
  （`{"headers":{...},"code":200}`，无 `lwp`），应识别为 ACK 不要 warn。
- `WS 消息字段不完整` 30143 条/天：122 万条/天 xbroadcast 回放包解析出的伪消息。

**修复方向（未实施）**

1. `RECONNECT_DELAY` 5 → 0.5~1 秒（断线率 14% → ~3%），最低成本。
2. 预热重连：连接活到 ~30 秒即新建连接替换，实现零窗口（需验证平台是否允许短暂双连）。
3. 兜底对账（不依赖 WS）：修 acc2 订单接口权限 + 周期订单同步，才是可靠保险。
4. 裸 ACK 识别 + xbroadcast 回放包跳过，降低 15 万条/天日志与 CPU。

## 2026-10-26（修复）：自动发货补上"订单号 + 聊天消息兜底补发 + 可读失败原因"

针对上面两条 2026-10-26 记录的根因，本轮做了 5 处代码修复（提交见 `git log`）。

**1. 订单号解析（致命缺口）**

- 付款卡片的 `reminderUrl` 只有 `itemId/peerUserId/sid/messageId`，**没有 orderId**；
  订单号在 `rawPayload["1"]["6"]["3"]["5"]`（转义 JSON 的 dxCard，按钮 `idleDeliver?...&orderId=`）
  和 `rawPayload["1"]["10"]["extJson"]` 的 `updateKey`（`<sid>:<orderId>:...`）里。
- 新增 `app/services/payment_order_id.py`：`extract_order_id_from_text/payload/message`，
  支持 `orderId=`、`bizOrderId=`、`order_detail?id=`、`updateKey` 冒号段、`"orderId":`，
  递归 6 层、兼容"JSON 字符串套 JSON 字符串"。实测历史消息 `id=1100` 解析出
  `5127384999311028844`。
- `ws_protocol.parse_numbered_fields` 返回体新增 `orderId`，`ws_delivery_handler._process_delivery`
  按 `msg.orderId → extract_order_id_from_message → reminderUrl` 顺序取值。
  效果：`realtime_delivery_attempt.external_order_id` / `delivery_record.order_id` 不再恒为 NULL，
  幂等从"消息事件键"升级为"订单键"（同买家复购不会被误判重复）。

**2. 去重防线原本是死代码**

- 旧 `_has_existing_realtime_delivery` 查 `delivery_record.receiver_info` 的
  `sid/xyGoodsId/buyerUserId`，但实时发货写入的 `receiver_info` 只有
  `{"realtimeAttemptId": N}` → 永远命中 0 行，等于没有防线。
- 改为 `_find_duplicate_delivery`：有订单号按 `realtime_delivery_attempt.external_order_id`
  查（`superseded` 忽略；`failed & retry_safe=1` 交给状态机重试；其余状态拦截）；
  无订单号退化为"会话+商品+买家"10 分钟窗口（`@goofish` 后缀双向归一）。

**3. 无规则命中时给出可读原因**

- `_diagnose_missing_rule()` 输出 `goods_not_found / config_missing / config_disabled /
  mode_unsupported / card_group_missing / unknown`，日志变成
  `Realtime delivery no rule matched ... reason=config_disabled`。
  以后"页面显示启用但不发货"能一句话定位。

**4. 聊天消息兜底补发通道（新）**

- 账号 2 的「已售出订单」接口返回 `PERMISSION_EXCEPTION::无权限访问`，`xianyu_trade_order`
  一条都没有，原 `delivery_recovery`（按订单表扫）对它永远无效。
- 新增第二通道：直接扫 `xianyu_chat_message`（`content_type=26 & direction='IN' &
  reminder_content LIKE '%已付款%'`，默认回看 12 小时、每次 20 条、至少 300 秒前），
  找出没有 success/pending 类 attempt 的付款消息，用 `_process_delivery` 重跑一遍状态机。
- 只重试"发送前失败"的旧尝试（`delivery_mode_unavailable` / `card_group_missing` /
  `delivery_content_empty` 或 `retry_safe=1`），先把旧 attempt 置为
  `state='superseded'`（`event_key` 加 `sup:` 前缀避免唯一键冲突）；已发送/未知态一律不动。
- 开关/参数（`.env`，可选）：`DELIVERY_RECOVERY_CHAT_ENABLED`（默认 true）、
  `DELIVERY_RECOVERY_CHAT_LOOKBACK_HOURS`（默认 12，1~168）、
  `DELIVERY_RECOVERY_CHAT_BATCH_SIZE`（默认 20，1~100）。
- ⚠️ 风险：系统无法感知"用户在闲鱼 App 里手工发过卡"的单子，回看窗口内的老单会被补发一次。
  如已人工补发过，把窗口调小或临时 `DELIVERY_RECOVERY_CHAT_ENABLED=false`。

**5. INFO 日志进不了 journald**

- `uvicorn` 启动时没有 `basicConfig`，root logger 默认 WARNING，`WS 连接成功` /
  `Realtime delivery ...` / `delivery_recovery ...` 全部看不到，排障全靠猜。
- `app/main.py` 增加 `_configure_logging_once()`：无 handler 时 `basicConfig`，
  `LOG_LEVEL` 默认 `INFO`，并统一 `root.setLevel`。

**运维提醒**

- 代码不能替用户打开配置：`delivery_goods_config` 里 goods 17（`1094823097374`）、
  goods 25（`1091821311650`）的 `payDelivery.enabled=0`（10-26 11:37:55/11:37:59 被保存动作写成停用，
  与 09:21 那次同因），另外 `1092539311888` / `1092046159577` / `1092280824859` / `1094895404770`
  根本没有配置行。必须在「自动发货」页面重新选「启用」并保存。
- 前端「启用/停用」是 `<select>`，鼠标滚轮/键盘很容易静默改值；后续建议换 checkbox + 保存前提示。
