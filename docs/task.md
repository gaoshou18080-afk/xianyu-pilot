# 任务进度索引（docs/task.md）

> 文档约定见仓库根 `AGENTS.md`。本文件记录任务进度、重要 bug、反复踩坑与关键业务逻辑索引。

## 项目概览

- 项目：xianyu-pilot（闲鱼助手 开源版，单用户）
- 技术栈：Python 3.12 + FastAPI（apps/api）、Node 22 + Express/Playwright（apps/crawler）、Vue3 + Vite 8（apps/web）、MySQL 8、Redis 7
- 本地开发：裸机非 Docker，使用 `scripts/setup-local.sh` + `start-local.sh` /
  `stop-local.sh` / `status-local.sh`
- 服务器 C：**正式裸机发布，非 Docker**，systemd 托管 API/Worker/Crawler/Redis，
  Nginx 托管 Web

## 部署进度

| 时间 | 事项 | 状态 |
| --- | --- | --- |
| 2026-10-05 | 克隆仓库到 `/var/www/xianyu2`（commit `12c6644`） | 完成 |
| 2026-10-05 | 安装 Node.js 22.23.1 到 `/opt/node22`（系统 `/usr/local/bin` 软链） | 完成 |
| 2026-10-05 | 运行 `scripts/setup-local.sh`：生成 `.env`/`.venv`/依赖/admin hash | 完成 |
| 2026-10-05 | 手工创建 MySQL 库 `xianyu_opensource` 与用户 `xianyu`（生成随机密码） | 完成 |
| 2026-10-05 | 数据库迁移 45 个版本全部 applied，共 73 张表 | 完成 |
| 2026-10-05 | 安装 Playwright Chromium v1228（匹配 playwright 1.61.1） | 完成 |
| 2026-10-05 | 启动 API/Crawler/Web/Scheduler 并健康检查通过 | 完成 |
| 2026-10-05 | 选定服务器 C（`104.234.174.250`）并完成裸机发布前评估 | 完成，存在内存阻塞 |

### 服务器 C 正式发布

| 时间 | 事项 | 状态 |
| --- | --- | --- |
| 2026-10-06 | 安装 Node.js 22.23.1 / npm 10.9.8 | 完成 |
| 2026-10-06 | 克隆代码 `12c6644`，上传 Web/Crawler 本地构建产物 | 完成 |
| 2026-10-06 | 创建生产 `.env`、secrets、Python venv | 完成 |
| 2026-10-06 | 创建 MySQL 库/用户并执行 45 个迁移 | 完成 |
| 2026-10-06 | 创建独立 Redis `127.0.0.1:16379` | 完成 |
| 2026-10-06 | 安装 Crawler 生产依赖和 Playwright Chromium | 完成 |
| 2026-10-06 | 安装并启用 API/Worker/Crawler/Redis systemd | 完成 |
| 2026-10-06 | 配置并 reload Nginx 站点 | 完成 |
| 2026-10-06 | API readiness、Worker heartbeat、Crawler `/ready` | 全部通过 |
| 2026-10-06 | 真实 Chromium 启动与内存冒烟测试 | 通过，峰值期间可用约 1.68GB |
| 2026-10-06 | 经服务器 C 测试首页、healthz、readyz、admin 登录 | 全部通过 |
| 2026-10-06 | DNS 切换到服务器 C | 完成，公共与权威解析均为 `104.234.174.250` |
| 2026-10-06 | 使用 Cloudflare DNS-01 申请 HTTPS 证书 | 完成，续期演练通过 |
| 2026-10-06 | 公网 HTTP/HTTPS/readiness 验收 | 通过，HTTP 301，HTTPS 200，readyz ready |
| 2026-10-06 | 提交服务器 C 部署配置与文档 | 已本地提交，推送被 GitHub 403 拒绝 |

### 线上 AI 自动回复排查

| 时间 | 事项 | 状态 |
| --- | --- | --- |
| 2026-10-06 | 核对两个商品作用域与线上 AI 配置 | 两个商品均开启；AI 主开关为 `enabled=true` |
| 2026-10-06 | 核对模型是否实际参与自动回复 | 线上 `ai_auto_reply_attempt` 总数为 0，从未进入模型调用 |
| 2026-10-06 | 定位第一个商品无 AI 回复原因 | 人工接管 30 分钟策略和迟到消息保护在模型调用前拦截 |
| 2026-10-06 | 核对第二个商品真实消息链路 | 无会话和消息，尚未形成可验证的真实请求 |
| 2026-10-06 | 检查 WebSocket 同步包 | 定位嵌套 `content` 被解析为伪消息并持续回放，已修复并通过回归测试 |
| 2026-10-06 | 排除服务器时区影响 | C 为 `+07`；北京时间 18:48 对应服务器 17:48，AI 按 `Asia/Shanghai` 且 24 小时工作，时区不触发拦截 |
| 2026-10-06 | 验证 18:48 消息是否落库 | 未落库；消息表最新记录仍为服务器约 17:24，确认故障发生在 WebSocket 解析入库前 |
| 2026-10-06 | 发布 WebSocket 解析修复 | `8b382de` 推送至 fork，C 快进更新并重启 API，readiness=`ready` |
| 2026-10-06 | 观察线上解析告警 | 重启后 95 秒内旧四类字段错位告警为 0；新消息 `id=188` 正常落库且无伪消息成对出现 |
| 2026-10-06 | 端到端 AI 回复验证 | 待目标商品产生一条非人工接管窗口内的新买家消息 |

### 自动发货卡密消息错乱修复

| 时间 | 事项 | 状态 |
| --- | --- | --- |
| 2026-10-08 | 用户反馈付款后买家收到的是 CSV 表头（`商品,批次,状态,备注,...`）而非配置的卡密模板 | 已复现，定位根因 |
| 2026-10-08 | 根因 1：`_load_goods_delivery_rule` card 模式只读 `cardTemplate`，旧配置把模板存在 `content` 字段导致回退到 `{卡密}` 占位符 | 已修复，两处（商品通用 + SKU 覆盖）都加 content 回退 |
| 2026-10-08 | 根因 2：`realtime_delivery` 不识别用户写的 `{卡密占位}`，且 `{卡密}` 是其子串，会渲染出 `<secret>占位}` 的脏输出 | 已修复，按长度优先替换 `{卡密占位}`/`{卡密}`/`{kmKey}` |
| 2026-10-08 | 根因 3：用户导入卡密时把 CSV 整行（含表头）当作卡密内容存到 `card_item.card_value`；第一条 id=1 是 CSV 表头，已被发货消费 | 待用户确认是否清洗剩余 8 张卡的 `card_value`（目前代码 `card_value or card_key` 优先取 value，会继续发 CSV 行而非卡号 `YF-XXXX-XXXX`） |
| 2026-10-08 | 提交修复 `91aff94`，新增 10 个回归测试 | 已推送 fork，C 已 fast-forward 并重启 API/Worker，readiness=ready |

### 卡密自动发货「已启用但不生效」调查（服务器 C，2026-10-26）

| 时间 | 事项 | 状态 |
| --- | --- | --- |
| 2026-10-26 09:23 | 买家付款消息 `xianyu_chat_message.id=869`（商品 `1091821311650`）未自动发货，`realtime_delivery_attempt.id=27` = failed / `delivery_mode_unavailable`，日志 `Realtime delivery no rule matched` | 已复现 |
| 2026-10-26 | binlog 复盘：09:21:16（goods 17）与 09:21:23（goods 25）两次后台保存把 `payDelivery.enabled` 由 1 写成 0，其余字段完全一致 | 根因 |
| 2026-10-26 | 09:49:43 / 09:49:50 用户重新启用（enabled 0→1）；此后无新付款事件，故 09:23 之后未再验证 | 待真实订单验证 |
| 2026-10-26 | 线上只读脚本复算规则：goods 17/25（`1094823097374`/`1091821311650`）均命中 ruleId 7/8、mode=card、cardGroupId=4、模版 133 字；卡密分组 4 可用 19 张 | 配置正确 |
| 2026-10-26 | 商品 `1094895404770`（goods 16）从未保存过发货配置，08:37、23:13 两次失败均为 unconfigured | 需用户重配 |
| 2026-10-26 | 建议改进：保存配置时记录 `enabled` 旧值/新值审计日志；启用开关改用 checkbox/防滚轮误改 | 待排期 |

### 货源库商品列表显示闲鱼商品ID

| 时间 | 事项 | 状态 |
| --- | --- | --- |
| 2026-10-25 | 用户反馈：货源库「已配置商品 / 商品列表」中商品名下一行显示的是 `category`（闲鱼类目ID，如 `50023914`，所有商品相同），不是商品ID | 已定位 |
| 2026-10-25 | 改为显示 `external_goods_id`（闲鱼 itemId，如 `1094823097374`）：API `_goods_record` 增加 `externalGoodsId`，选列增加 `g.external_goods_id`/`g.goods_id` | 已修复 |
| 2026-10-25 | 商品搜索支持按商品ID匹配（`_goods_search_clause` 增加 `external_goods_id`/`goods_id`），前端搜索提示改为「商品标题 / 商品ID」 | 已修复 |

## 访问地址

- Web：http://127.0.0.1:15176/#/login
- API 健康：http://127.0.0.1:15177/health/ready
- Crawler 就绪：http://127.0.0.1:15178/ready
- 默认账号：`admin` / `admin123`（首次启动生成，请登录后修改）
- 服务器 C 临时验证：
  `curl --resolve xianyu.enjoygoo.win:443:104.234.174.250 https://xianyu.enjoygoo.win/readyz`
- 正式域名：https://xianyu.enjoygoo.win（已切换到服务器 C，公网验收通过）

## 常用命令

```sh
cd /var/www/xianyu2
sh ./status-local.sh   # 查看状态
sh ./stop-local.sh     # 停止服务
sh ./start-local.sh    # 启动（端口被占用时会失败，需先 stop）
tail -f output/local-dev/api.out.log
```

## 踩坑记录

1. **仓库根目录已有 `AGENTS.md` 时 `git clone .` 会失败**：需先把 `AGENTS.md` 移开再 clone，clone 完再移回。
2. **Node 版本必须 22**：`apps/web/.npmrc` 设了 `engine-strict=true`，且 Vite 8 要求 Node ≥22；系统自带 Node 18 会导致 `npm install` 失败。已安装 22.23.1。
3. **Playwright 浏览器版本需匹配**：`~/.cache/ms-playwright` 里预装的 chromium-1243 与 crawler 的 playwright 1.61.1 不匹配，crawler `/ready` 报 `playwright chromium executable is unavailable`。需在 `apps/crawler` 执行 `npm exec playwright install chromium` 装 v1228。
4. **MySQL root 不可用**：`setup-local.sh` 默认用 root 建库会失败；本机 `dba_admin/123456` 具备 CREATE USER/GRANT 权限，改用 dba_admin 手工建库建用户（密码取 `.env` 中生成的 `MYSQL_PASSWORD`）。
5. **后台进程会被会话清理**：用 `start-local.sh` 内部的 `nohup` 不够，需外层 `setsid` 启动才能脱离会话存活。
6. **AI 配置测试不等于自动回复链路可用**：测试接口只验证模型；运行时还会经过作用域、会话暂停、迟到保护、30 分钟人工接管、额度等门禁。
7. **会话自动恢复时间与运行时人工接管时间不一致**：会话状态机约 60 秒恢复，但策略默认仍按 30 分钟拦截。

## 问题索引

- AI 自动回复被人工接管/迟到保护拦截、WebSocket 字段错位：
  见 `docs/bugs.md` 的 2026-10-06 记录。

## 待办 / 说明

- 当前 Web 为 Vite dev server（`npm run dev`），生产如需静态资源可 `npm --prefix apps/web run build` 后由 nginx 托管。
- `runtime-status` 中 `commercialBridgeMode=commercial` 且探活失败（约 10s 超时），因商业版桥接未配置 token；不影响本地功能，如需纯本地兜底可在 `.env` 关闭商业版桥接相关配置。
- 开发机本地脚本未配置 systemd 自启；服务器 C 已启用 systemd 自启。
- 服务器 C 裸机发布评估见 `docs/report/server-c-baremetal-preflight-2026-10-05.md`：当前可用内存约 399MB、Swap 为 0，最低需 4GB Swap 和 1.2GB 可用物理内存并限制 Crawler 并发为 1。
- DNS 已切换到 `104.234.174.250`；Cloudflare 权威 NS、1.1.1.1 和 8.8.8.8
  公共解析均已确认。
- `gaoshou18080-afk` 对 `dameng2026/xianyu-pilot` 没有写权限，已创建并推送至
  `gaoshou18080-afk/xianyu-pilot` fork；服务器 C 已添加 `fork` 远端并从该远端发布。
- 服务器 C 的详细运维命令见 `docs/manual.md`，本次发布记录见
  `docs/deploy.md`，上线结果见
  `docs/report/server-c-baremetal-deployment-2026-10-06.md`。
- 仓库缺少全局约定要求的 `docs/important.md`，正式部署前需恢复或确认无遗漏决策。

## WS 每 40 秒断线调查（2026-10-26）

- 结论：平台侧约 35 秒会话上限，6 组只读探针复现（/var/www/xianyu 未改代码），
  客户端参数无法延长；**禁止周期性重新 `/reg`**（会立即断线）。
- 影响：每账号每天约 1587 次重连、11~14% 时间离线；重连只回放 xbroadcast 广播包，
  无聊天消息补发 ⇒ 窗口内付款通知永久丢失。
- 待决策修复优先级：① `RECONNECT_DELAY` 5→1 秒 ② 预热重连（零窗口）
  ③ acc2 订单同步权限 + 周期对账兜底 ④ 日志降噪（裸 ACK / xbroadcast）。
- 详见 `docs/bugs.md` 的 2026-10-26「WebSocket 每 40 秒固定断线」。

## 自动发货漏单修复（2026-10-26 第二轮，已发布）

- 提交内容（本次）：
  1. `services/payment_order_id.py`（新增）：从付款卡片 rawPayload 解析真实订单号；
  2. `ws_protocol` / `ws_delivery_handler`：消息体带 `orderId`，attempt/record 记录订单号；
  3. 去重防线重写为 `_find_duplicate_delivery`（原实现查 `receiver_info` 是死代码）；
  4. `_diagnose_missing_rule`：无规则命中时日志给 `reason=config_disabled` 等可读原因；
  5. `delivery_recovery` 新增「按付款卡片消息补发」通道（acc2 无订单接口权限的兜底），
     worker 每 600s 跑一轮，默认回看 12 小时、每轮 20 条；并加"付款后 6h 内有卖家 OUT 消息
     就跳过"的人工已回复安全阀（防止给已手打发卡的买家重复发卡）；
  6. `main.py` 配 `basicConfig`，INFO 日志进 journald。
- 发布步骤：本地测试 31 passed → commit → push `fork main` → 服务器
  `git pull --ff-only fork main` + `systemctl restart xianyu-api xianyu-worker`。
- **需要人工操作**：在「自动发货」页面给 goods 17(`1094823097374`) / 25(`1091821311650`) 重新选
  「启用」并保存；其余在售商品（`1092539311888`、`1092046159577`、`1092280824859`、`1094895404770`）
  需先保存一份卡密发货配置，否则永远是 `reason=config_missing`。
- 观测手段：
  - `journalctl -u xianyu-api -u xianyu-worker --since "10 min ago" | grep -E "reason=|delivery_recovery|订单号"`
  - `select id,external_order_id,state,last_error_code from realtime_delivery_attempt where created_time>=CURDATE() order by id desc limit 20;`
    预期 `external_order_id` 非空。
- 风险：安全阀只能靠"会话里有没有卖家 OUT 消息"判断人工是否已发货，仍有概率漏判/误判；
  必要时调 `DELIVERY_RECOVERY_CHAT_LOOKBACK_HOURS`、`DELIVERY_RECOVERY_CHAT_SKIP_HUMAN_REPLY`
  或直接 `DELIVERY_RECOVERY_CHAT_ENABLED=false`。
- 详细背景见 `docs/bugs.md` 的三条 2026-10-26 记录（配置被写成停用 / 漏单机制缺口 / 本轮修复）。

## 自动发货与"人工干预"关系调查（2026-10-28，只读，无代码改动）

- 结论：**实时自动发货没有任何"人工干预/人工说话就不发货"的逻辑**；只有兜底补发的「聊天消息通道」有 6 小时卖家 OUT 消息安全阀，兜底「订单通道」没有。
- AI 自动回复的三层人工接管（`auto_reply_paused` 60 秒、`humanInterventionPauseMinutes` 默认 30 分钟、`mode=manual`）与发货完全解耦。
- 线上实证：`realtime_delivery_attempt.id=40`（会话 `68535169526`）在卖家人工回复"对，官方中转"1 分钟后买家付款，系统照常自动发卡；attempt 25/29/30 同样如此。
- 附带发现：生产库 `auto_reply_paused=1` 为 0 条、`last_manual_reply_at` 全 NULL，人工 OUT 暂停逻辑实际未落库（待修）。
- 详见 `docs/business/auto-delivery.md`。

## 自动发货"秒级"延迟调查（2026-10-28，只读，无代码改动）

- 实测：平台付款 → 我们落库 p50 约 10s、p90 约 30s（acc3 今日平均 18s / max 33s）；我们内部链路 0~1s。
- 根因①`ws_client.py` 的 `_sync_pts` 永不前进（`maxPts/highPts` 解析位置错误）→ 每 35s 重放 77~102 条老消息，
  付款卡片要等窗口滑到（10~30s）。根因②每 32s 主动重连的离线窗口 p50 2s / p90 9s / max 13s（close 等 5s+5s）。
- 修复计划 P0 游标 / P1 close 卡顿 / P2 处理解耦 / P3 预热双连 / P4 HTTP 轮询兜底；验证指标：
  `收到 N 条消息` 降到 0~3 条、`created_time-message_time` p90 < 3s。
- 详见 `docs/bugs.md` 2026-10-28 条目。

## 2026-10-29 acc3 测试单无反应：WS 游标冻结 + 订单兜底三处缺陷（已修复）

- 现象：acc3 商品 `1096807048573` 已配自动发货，01:01:01 付款，9 分钟无反应。
- 平台侧订单真实存在（`5127895009054122526`，待发货）；我们库里没有消息、没有订单、没有 attempt。
- 四处缺陷：WS `_sync_pts` 冻结回放old消息 / acc3 无订单同步数据源 / `paySuccessTime` 没解析 / 时间 +08→+07 错位 /
  `xianyu_conversation` 无 `deleted` 列导致订单兜底必然异常。
- 已修复：游标推进、时间字段与时间区转换、会话查询 SQL、补发循环内嵌 HTTP 订单同步、AI 陈旧消息守卫。
- 详见 `docs/bugs.md` 2026-10-29 条目。
