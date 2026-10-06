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
