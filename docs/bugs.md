# 问题与踩坑记录（docs/bugs.md）

## 2026-10-29：迁移到 Y 后应用账号无法创建运行时表

**现象**

- API readiness 为 ready，但日志持续出现
  `CREATE command denied to user 'xianyu_app'@'localhost'`，目标表为
  `xianyu_captcha_backoff`。
- 迁移脚本原先只创建并授权了 `xianyu_app@127.0.0.1`。

**原因**

- `.env` 虽然连接 `MYSQL_HOST=127.0.0.1`，MySQL 仍将连接识别为
  `xianyu_app@localhost`，实际使用 `localhost` 身份。
- 应用启动时会 `CREATE TABLE IF NOT EXISTS` 滑块退避表，仅 CRUD 权限不足。

**处理**

- 创建同密码的 `xianyu_app@localhost`，只授权
  `xianyu_opensource.*` 的 `SELECT/INSERT/UPDATE/DELETE/CREATE/ALTER/INDEX`。
- 重启后表成功创建，API 日志不再出现权限拒绝。

## 2026-10-29：TLS 证书迁移后 certbot 无法续期

**现象**

- Y 已有证书、私钥和 renewal 配置，`certbot renew --dry-run` 仍要求注册账户。
- 首次安装 certbot 后没有 `/etc/letsencrypt/accounts` 中的账户数据。

**处理**

- 从 W 迁移 `/etc/letsencrypt/accounts`，保留 `root:root` 和 `private_key.json`
  的 `0400` 权限。
- Y 安装 `certbot python3-certbot-dns-cloudflare`，使用
  `--no-random-sleep-on-renew` 完成 DNS-01 dry-run。

**教训**

- TLS 迁移必须同时检查 live、archive、renewal、accounts 和 DNS 凭据，不能只看
  当前证书文件是否存在。

## 2026-10-29：Y 服务本机正常但公网 80/443 超时

**现象**

- Y 本机 Nginx 监听 `0.0.0.0:80/443`，回环 HTTPS readiness 正常。
- 开发机、W、C 及三个外部检测节点连接 Y 的 80/443 全部超时。

**原因与处理**

- Ubuntu UFW 实际未启用，问题位于阿里云安全组；放行入方向 TCP 80/443 后恢复。
- 切换 DNS 前必须从至少一个非目标节点验证公网端口，不能只测 `127.0.0.1`。

## 2026-10-29：阿里云杭州节点未备案域名公网被拦截

**现象**

- 安全组放行 80/443 后，Y 本机访问自己的公网 IP 得到 HTTP 301 和 HTTPS 200。
- 开发机和 W 访问时，80 返回 `403 Forbidden`、`Server: Beaver`，页面标题为
  `Non-compliance ICP Filing`；443 在 TLS ClientHello 阶段被重置。
- Y 本机 `nginx -T`、HTTP Host 回源和 HTTPS `/readyz` 均正常，证明拦截发生在
  阿里云网络入口，不在 Nginx 或应用。

**处理约束**

- 不通过非标准端口、反向代理或其他方式规避备案要求。
- 由域名负责人完成 `enjoygoo.win` 的 ICP 备案接入，或把站点切换到已备案域名。
- 完成后必须从开发机和 W 两个外部节点重新验证，禁止仅凭 Y 本机公网回环判定通过。

## 2026-10-29：付款卡片到达延迟 0~30 秒（自动发货"不快"），WS 一条会话只同步一次

**现象**

- 用户端：闲鱼付款后自动发货要等 20 秒以上，别人家"秒级"。
- 实测（订单 `5127833412058086516`）：付款 `00:31:46.8` → WS 落库 `00:32:05.4`（18.6s）
  → 规则命中 `00:32:06.5` → 卡密发出 `00:32:07.8`。**落库→发卡只有 1 秒**。

**根因**

1. `/r/SyncStatus/ackDiff` 是**长轮询**：无数据时请求被服务端挂住 15~30 秒；
   而回包大多不带 `lwp=/r/SyncStatus/ackDiff`，`_handle_sync_package` 里
   `if is_ack_diff: await self._do_sync()` 的链式重同步**几乎不触发** ⇒ 每条
   32 秒会话只发出 1 次同步请求，付款卡片只能等下一批（下一条文本消息）捎带下发。
2. `_ThreadedWebSocketAdapter.close()` 等待 5s(close_event) + 5s(thread.join)，
   每 32 秒会话回收实际丢 ~8 秒入站消息（离线率 ~25%）。
3. 一单付款会连着收到 2 条付款相关消息，并发进入 `_process_delivery`，
   两次去重查询都在对方写库前完成 ⇒ 同订单 2 个 attempt（`47 failed unconfigured`
   + `48 success`）。

**修复**

- `ws_client._sync_loop`：独立定时器每 2 秒主动 ackDiff（`WS_SYNC_POLL_INTERVAL_SECONDS`，
  默认 2），`_sync_outstanding` 连续 5 个无回包才让位；
- `close()` 等待 5+5 → 1+1 秒；`RECONNECT_DELAY` 1 → 0.2 秒；
- `ws_delivery_handler`：`_process_delivery` 外层加 `accountId+orderId` asyncio 串行锁；
- 去重：`delivery_mode_unavailable` 且 attempt 在 180 秒内 → 允许重试（商品晚于
  付款消息落库的竞态）。

**教训**

- 看到"消息到达慢"先量 `xianyu_chat_message.created_time - message_time`，
  不要怀疑发货逻辑（发货本身 0~1 秒）。
- 平台会攒批下发系统卡片（contentType=26），系统卡片本身不唤醒长轮询。

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
- **人工已回复安全阀（关键）**：付款后 6 小时内同一会话只要出现卖家 `OUT` 消息，就判定这单
  已人工手工发货，跳过不补。依据：2026-10-26 当天 3 个"失败"订单（18:41 `68536498773`、
  18:52 `68233063241`、09:23 `68212471464`）的买家其实都已收到人工发的 `sk-...` key，
  无脑补发会重复发卡。被挡掉的条数记在 `chatSkippedHumanReply` 统计里。
- 开关/参数（`.env`，可选）：`DELIVERY_RECOVERY_CHAT_ENABLED`（默认 true）、
  `DELIVERY_RECOVERY_CHAT_LOOKBACK_HOURS`（默认 12，1~168）、
  `DELIVERY_RECOVERY_CHAT_BATCH_SIZE`（默认 20，1~100）、
  `DELIVERY_RECOVERY_CHAT_SKIP_HUMAN_REPLY`（默认 true，设 false 表示无视人工回复强行补发）。
- 残余风险：如果人工是在**别的设备/系统外**发的货且没在闲鱼里回消息，仍可能补发一次；
  反之如果自动回复机器人对付款消息回过一句话，该单会被安全阀挡住（宁可漏补，不重复发卡）。

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

## 2026-10-28：自动发货延迟 10~33 秒（"别人秒级"）—— 根因是 WS 同步游标冻结 + 重连离线窗口

**实测基线（服务器 C，只读查询）**

- 平台消息时间 → 我们落库时间（`xianyu_chat_message.created_time - FROM_UNIXTIME(message_time/1000)`，
  近 3 天 contentType=26 的付款卡片）：
  acc1 平均 8.4s / max 13s；acc2 平均 49.4s / max 1967s（1 条丢后补）；
  acc3 平均 18.4s / max 33s（10 条里只有 1 条 ≤5s）。
- 我们自己的链路（落库 → attempt → 发货消息）一直是 **0~1 秒**，慢的不是这段。

**根因 1：ackDiff 同步游标不前进，每轮重放 77~102 条老消息**

- `ws_client.py:1375` 只在 `_sync_pts<=0` 时赋一次值（进程启动时刻），此后永不更新；
  `ws_client.py:1610-1635` 从 `body.syncPushPackage.maxPts/maxHighPts` 取水位，而该字段实际不存在
  （日志里从无 `maxHighPts` 输出，`WS 同步请求已发送 ... pts=1793176969083256 highPts=0` 恒定）。
- 结果：每 35 秒的每轮同步都从"进程启动时刻"起回放满 100 条上限，日志稳定 `WS 收到 102 条消息`、
  `WS 收到 77/79 条消息`；而 DB 每小时只新增几十行（acc2 今日 15:00-17:00 共 101 行）→ 99% 是重复回放。
- 新到的付款卡片只能等重放窗口滑到才出现（acc3 20:28:00.78 的付款卡片 20:28:10.1 才收到），
  这就是 10~30s 延迟的直接来源。

**根因 2：每 32 秒主动重连的离线窗口偏长**

- `_ThreadedWebSocketAdapter.close()` 等 `close_event` 5s + `thread.join` 5s（`ws_client.py:248-268`）。
- 实测 acc2（19:00 起 159 个周期）：主动重连 → 注册成功 的离线时长 p50 **2.0s**、p90 **9.0s**、
  max **13s**、平均 **4.0s**，45% 的周期 >2s。加上 `RECONNECT_DELAY=1` 与注册 1.1s，
  离线占比约 11%。

**附带**

- `_process_message` 在接收循环里串行 await（DB + outbox + SSE 广播），重复消息也全额走一遍查询，
  会造成队头阻塞。
- 今日 19:15~20:29 的 4 单失败（attempt 41-44）与延迟无关，是 acc3 商品 `1096811748835`
  `config_missing` → `delivery_mode_unavailable`。

**修复方向（未实施）**

1. P0 修正游标：每轮用已收消息最大 `messageTime`+1µs（或 now）作为 `pts`，并从响应任意层正确解析
   `maxPts/highPts` 推进；保留 pnmId 去重 + 现有对账兜底防漏。预期付款卡片延迟 → <1s。
2. P1 消除 close 卡顿：先 `sock.shutdown`/`close_frame` 立即返回，不再等 5s+5s；`RECONNECT_DELAY` 1→0.2。
   预期离线 4.0s → ~1.3s。
3. P2 处理解耦：批量按 `pnm_id` 预查重，重复直接丢弃（不写库不发 SSE）；付款类消息优先触发发货。
4. P3/P4（可选）：预热双连接（需探针验证同账号并发不被互踢）；3~5s HTTP 轮询兜底把最坏时延锁死。

### 2026-10-29：acc3 测试拍单付款 9 分钟完全没反应——4 个独立缺陷叠加

用户已给「高手在仙界」(acc3) 商品 `1096807048573` 配好自动发货（`delivery_goods_config.id=22`，
`updated_time=2026-10-28 23:58:48`），01:01:01 拍单付款成功，系统无任何反应。

排查结论（线上实测，只读）：

1. **订单真实存在**：`fetch_sold_orders_page(3)` 直连成功，返回
   `orderId=5127895009054122526 / itemId=1096807048573 / 待发货 / paySuccessTime=2026-10-29 01:01:01`。
   → 不是"没付款"，也不是"配置没生效"。
2. **WS 同步游标冻结**：`WS 同步请求已发送 pts=1793189669868316` 每 32 秒恒定不变，
   每轮 `WS 收到 103 条消息` 全是重复回放；`xianyu_chat_message` 自 21:37 后无新行
   （19点49条 / 20点38条 / 21点4条 / 22点0条 / 23点3条），付款卡片根本没进库。
3. **acc3 没有订单同步数据源**：`xianyu_trade_order` acc3 长期 0 行；`scheduled_task` 里
   只有 acc2 的每日 02:05 同步（且 COOKIE_EXPIRED）。acc3 只能靠"WS 收到消息"触发订单同步，
   而 19:15/19:22/19:40/20:28 四次触发都返回 `PLATFORM_UNAVAILABLE`。
   手工调 `POST /api/order/syncSoldOrders` 立即可用 → 一次补回 12 单。
4. **订单字段解析错**：`xianyu_order_sync._parse_order_time` 读 `payTime/paymentTime`，
   平台实际返回 `paySuccessTime` → `pay_time` 恒 NULL，而补发扫描 `WHERE pay_time IS NOT NULL` 直接跳过。
5. **时区错位**：平台时间是北京时间(+08)，服务器/DB 是 +07，`pay_time` 落库后比 `NOW()` 快 1 小时，
   `pay_time <= NOW()-300s` 恒不成立 → 订单兜底要等 1 小时后才可能生效。
6. **补发通道 SQL 报错**：`delivery_recovery._resolve_session_id` 查 `xianyu_conversation WHERE deleted = 0`，
   该表**没有 deleted 列** → 每个订单都 `OperationalError 1054` 失败（订单兜底从来没成功过）。

修复（本次已提交）：

- `ws_client.py`：新增 `_extract_sync_watermarks`（递归找 maxPts/highPts），
  每批消息用 `max(messageTime)*1000+1000` 推进 `_sync_pts` → 游标不再冻结，重连空窗期的消息
  下一轮同步能补回来。
- `xianyu_order_sync.py`：`pay_time` 读 `paySuccessTime`；时间按 +08 解析后转服务器本地时区。
- `delivery_recovery.py`：会话查询去掉不存在的 `deleted` 列，按规范化对端 ID 匹配、优先命中同商品会话；
  新增 `sync_delivery_account_orders()`，补发循环前先用 HTTP 订单接口同步一次（WS 挂了也有数据源）。
- `ws_startup.py`：AI 分支增加陈旧消息守卫 `ai_reply_max_message_age_seconds=900`，
  防止游标推进后一次性回放历史消息导致半夜给买家补发一堆 AI 回复（发货分支不受限）。
- `worker.py` + `config.py`：`delivery_recovery_order_sync_enabled`（默认 true）。

### 2026-10-29：服务器 C -> W 切换后账号 1 会话过期

- 停写快照、Redis 和 Cookie 完整迁移后，W 启动时账号 2、3 可连接；账号 1
  调用 Token API 返回 `FAIL_SYS_SESSION_EXPIRED::Session过期`，自动滑块也返回
  `SESSION_EXPIRED`。
- C 停用前账号 1 的 WS 仍在线，不能证明其 Cookie 能在新出口 IP 上重新换取 Token。
- 处理：在业务界面重新登录账号 1 并完成滑块/授权，再观察 `WS 已连接 accountId=1`。
- 禁止通过重新启动 C 与 W 双活来恢复该账号，否则会重复消费消息和发货。
