# 发布操作记录（docs/deploy.md）

> 约定：凡是发布内容里有「除代码更新以外」的操作（SQL/服务/目录/初始化/配置等），
> 必须在本文件登记，格式：`git 版本号 <=> 对应的详细发布过程及命令`。
> 只改代码的发布不用登记。

## 版本 `12c6644` <=> 服务器 C 正式裸机发布（非 Docker，2026-10-06）

环境：

- 本地：`/var/www/xianyu2`
- 服务器：C / `104.234.174.250`
- 目录：`/var/www/xianyu`
- 域名：`xianyu.enjoygoo.win`

本次发布包含代码、生产 secrets、MySQL 用户/数据库、Redis、systemd、Nginx、
Playwright 浏览器和目录权限等非代码操作。

### 1. 本地构建与发布包

```sh
cd /var/www/xianyu2
npm --prefix apps/web run build
npm --prefix apps/crawler run build
```

产物：

- `apps/web/dist/`，165 个文件
- `apps/crawler/dist/`，12 个文件

### 2. 服务器代码与运行时目录

```sh
sudo install -d -o guanli -g guanli -m 0755 /var/www/xianyu
sudo -u guanli git clone https://github.com/dameng2026/xianyu-pilot.git /var/www/xianyu
sudo -u guanli git -C /var/www/xianyu checkout 12c6644

scp -r apps/web/dist/* guanli@104.234.174.250:/var/www/xianyu/apps/web/dist/
scp -r apps/crawler/dist/* guanli@104.234.174.250:/var/www/xianyu/apps/crawler/dist/
```

服务器不执行 Vite/TypeScript 构建。安装 Node.js `22.23.1` 到
`/opt/node22`，npm 为 `10.9.8`。

### 3. Python venv 与生产 secrets

```sh
sudo -u guanli python3 -m venv /var/www/xianyu/.venv
sudo -u guanli /var/www/xianyu/.venv/bin/pip install \
  -r /var/www/xianyu/apps/api/requirements.txt
```

创建 `.env`、`secrets/` 和以下独立 secrets 文件，权限均为 `0600`：

```text
admin-password-hash
ai-provider-api-key
commercial-backend-access-token
cookie-crypto-secret
embedding-api-key
internal-api-token
jwt-secret
mysql-app-password
mysql-migration-password
mysql-root-password
redis-password
```

`.env` 权限为 `0600`。管理员初始凭据：`admin/admin123`；上线后必须立即修改。

### 4. MySQL（SQL）

创建数据库及两个最小权限运行时用户。密码从 secrets 文件读取，禁止写入文档：

```sql
CREATE DATABASE IF NOT EXISTS `xianyu_opensource`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'xianyu_app'@'127.0.0.1'
  IDENTIFIED BY '<mysql-app-password>';
CREATE USER IF NOT EXISTS 'xianyu_migrate'@'127.0.0.1'
  IDENTIFIED BY '<mysql-migration-password>';
GRANT SELECT, INSERT, UPDATE, DELETE ON `xianyu_opensource`.*
  TO 'xianyu_app'@'127.0.0.1';
GRANT ALL PRIVILEGES ON `xianyu_opensource`.*
  TO 'xianyu_migrate'@'127.0.0.1';
FLUSH PRIVILEGES;
```

执行迁移：

```sh
cd /var/www/xianyu/apps/api
../../.venv/bin/python -m app.migrations upgrade
```

结果：45 个迁移全部成功，schema 状态为 current。

### 5. 独立 Redis

创建 `redis` 用户可读的配置，不改动服务器既有 `6379` Redis：

```sh
sudo install -m 0640 -o root -g redis \
  deploy/server-c/xianyu-redis.conf \
  /etc/redis/xianyu.conf
sudo sh -c 'umask 0137; printf "requirepass %s\n" \
  "$(cat /var/www/xianyu/secrets/redis-password)" \
  > /etc/redis/xianyu-secret.conf'
sudo install -m 0644 \
  deploy/server-c/xianyu-redis.service \
  /etc/systemd/system/xianyu-redis.service
sudo systemctl daemon-reload
sudo systemctl enable --now xianyu-redis.service
```

监听：`127.0.0.1:16379`。

### 6. Crawler 运行时

```sh
cd /var/www/xianyu/apps/crawler
npm ci --omit=dev
npm exec playwright install chromium
```

实测浏览器：Playwright `1.61.1` + Chromium headless shell `1228` +
Chromium `149.0.7827.55`。生产配置限制
`CRAWLER_MAX_CONCURRENCY=1`、`CRAWLER_MAX_SESSIONS=10`、
`CRAWLER_FORCE_HEADLESS=true`。

### 7. systemd

```sh
sudo install -m 0644 deploy/server-c/xianyu-api.service \
  /etc/systemd/system/xianyu-api.service
sudo install -m 0644 deploy/server-c/xianyu-worker.service \
  /etc/systemd/system/xianyu-worker.service
sudo install -m 0644 deploy/server-c/xianyu-crawler.service \
  /etc/systemd/system/xianyu-crawler.service
sudo systemctl daemon-reload
sudo systemctl enable --now \
  xianyu-api.service xianyu-worker.service xianyu-crawler.service
```

API 和 Worker 最终移除了 `PrivateTmp=true`，保证两者共享
`/tmp/xianyu-scheduler-heartbeat`。该问题在首次发布中修复并重启验收。

### 8. Nginx

```sh
sudo install -m 0644 deploy/server-c/nginx-xianyu.enjoygoo.win.conf \
  /etc/nginx/sites-available/xianyu.enjoygoo.win
sudo ln -sfn /etc/nginx/sites-available/xianyu.enjoygoo.win \
  /etc/nginx/sites-enabled/xianyu.enjoygoo.win
sudo nginx -t
sudo systemctl reload nginx
```

Web 根目录：`/var/www/xianyu/apps/web/dist`。HTTPS 证书需等 DNS 切换到
`104.234.174.250` 后执行 `certbot --nginx -d xianyu.enjoygoo.win`。

实际发布采用 Cloudflare DNS-01，在 A 记录切换前完成证书签发：

```sh
sudo install -m 0600 -o root -g root \
  /home/guanli/migration-caipiao-from-b-20261002-200912/cert-extract/cloudflare.ini \
  /etc/letsencrypt/cloudflare.ini

sudo certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  --dns-cloudflare-propagation-seconds 30 \
  --cert-name xianyu.enjoygoo.win \
  -d xianyu.enjoygoo.win \
  --dry-run --non-interactive

sudo certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  --dns-cloudflare-propagation-seconds 30 \
  --cert-name xianyu.enjoygoo.win \
  -d xianyu.enjoygoo.win \
  --non-interactive
```

Nginx 改为 80 重定向到 443，443 使用
`/etc/letsencrypt/live/xianyu.enjoygoo.win/`。证书有效期为
`2026-10-06` 至 `2027-01-04`，`certbot renew --cert-name
xianyu.enjoygoo.win --dry-run --non-interactive` 已通过。

### 9. 验收

```sh
curl -fsS http://127.0.0.1:15177/health/ready
curl -fsS http://127.0.0.1:15178/ready
cd /var/www/xianyu/apps/api
../../.venv/bin/python -m app.worker --check

curl --resolve xianyu.enjoygoo.win:80:104.234.174.250 \
  http://xianyu.enjoygoo.win/
curl --resolve xianyu.enjoygoo.win:443:104.234.174.250 \
  https://xianyu.enjoygoo.win/healthz
curl --resolve xianyu.enjoygoo.win:443:104.234.174.250 \
  https://xianyu.enjoygoo.win/readyz
curl --resolve xianyu.enjoygoo.win:443:104.234.174.250 \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' \
  https://xianyu.enjoygoo.win/api/auth/login
```

结果：API readiness 全 `ok`，Worker heartbeat 新鲜，Crawler ready，首页
HTTP 200，登录 HTTP 200。使用 `--resolve` 验证 80 返回 301、443 HTTP/2
返回 200；证书已通过域名校验。真实 Chromium 启动测试期间可用内存约
`1675MB`，浏览器正常关闭。

### 10. 公网 DNS 验收

公共 DNS 已切换到 `104.234.174.250`，权威 NS、1.1.1.1 和 8.8.8.8 解析一致。
HTTPS 已通过 DNS-01 提前签发，公网验收结果如下：

```sh
dig +short @1.1.1.1 xianyu.enjoygoo.win A
# 104.234.174.250

curl -I http://xianyu.enjoygoo.win
# HTTP/1.1 301，Location: https://xianyu.enjoygoo.win/

curl -I https://xianyu.enjoygoo.win/healthz
# HTTP/2 200

curl -fsS https://xianyu.enjoygoo.win/readyz
# {"status":"ready"}
```

## 版本 `12c6644` <=> 开发机本地验证（历史，不用于服务器 C）

环境：开发机 `/var/www/xianyu2`，PHP 无关，栈为 Python/FastAPI + Node + MySQL + Redis。

### 1. 目录与代码

```sh
# 注意：根目录已有 AGENTS.md，clone 前先移开
cd /var/www/xianyu2
mv AGENTS.md /tmp/AGENTS.md.bak
git clone https://github.com/dameng2026/xianyu-pilot .
mv /tmp/AGENTS.md.bak ./AGENTS.md
```

### 2. 系统级依赖：Node.js 22.23.1

```sh
# 项目要求 node 22.23.1（apps/web/.npmrc engine-strict=true，Vite 8 需 >=22）
curl -fsSL -o /tmp/node-v22.23.1-linux-x64.tar.xz \
  https://nodejs.org/dist/v22.23.1/node-v22.23.1-linux-x64.tar.xz
sudo tar -xf /tmp/node-v22.23.1-linux-x64.tar.xz -C /opt
sudo ln -sfn /opt/node-v22.23.1-linux-x64 /opt/node22
for b in node npm npx corepack; do sudo ln -sfn /opt/node22/bin/$b /usr/local/bin/$b; done
```

### 3. 应用初始化（生成 .env / venv / 依赖 / admin hash）

```sh
cd /var/www/xianyu2
sh ./scripts/setup-local.sh </dev/null
# 生成：.env（随机 JWT/COOKIE/INTERNAL/MYSQL 密钥）、.venv、api/crawler/web 依赖、ADMIN_PASSWORD_HASH
# 默认管理员：admin / admin123
```

### 4. 数据库（SQL，setup 脚本因无法连接 root 未自动建库）

MySQL root 不可用；使用本地具备 `CREATE USER` / `GRANT` 权限的 DBA 账号手工创建。
密码取自 `.env` 的 `MYSQL_PASSWORD`：

```sql
CREATE DATABASE IF NOT EXISTS `xianyu_opensource` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'xianyu'@'localhost' IDENTIFIED BY '<.env 中 MYSQL_PASSWORD>';
CREATE USER IF NOT EXISTS 'xianyu'@'127.0.0.1' IDENTIFIED BY '<.env 中 MYSQL_PASSWORD>';
GRANT ALL PRIVILEGES ON `xianyu_opensource`.* TO 'xianyu'@'localhost';
GRANT ALL PRIVILEGES ON `xianyu_opensource`.* TO 'xianyu'@'127.0.0.1';
FLUSH PRIVILEGES;
```

```sh
# 数据库迁移（45 个版本）
cd /var/www/xianyu2/apps/api && ../../.venv/bin/python -m app.migrations upgrade
```

### 5. Playwright 浏览器（crawler 依赖）

```sh
# 预装 chromium-1243 与 playwright 1.61.1 不匹配，必须重装 v1228
cd /var/www/xianyu2/apps/crawler && npm exec playwright install chromium
```

### 6. 启动服务

```sh
cd /var/www/xianyu2
setsid nohup sh ./start-local.sh </dev/null >/tmp/start-local.log 2>&1 &
# 端口：Web 15176 / API 15177 / Crawler 15178；Scheduler 无端口
sh ./status-local.sh
```

### 验收

- `GET http://127.0.0.1:15177/health/ready` → 200，components 全 ok
- `GET http://127.0.0.1:15178/ready` → 200 ready
- `GET http://127.0.0.1:15176/` → 200
- `POST /api/auth/login`（admin/admin123）→ 200 返回 token

## 版本 `2ea1ec3` <=> 自动发货漏单修复发布 + 重新启用两个商品配置（2026-10-26）

代码更新：`git pull --ff-only fork main`（`bb8faeb..2ea1ec3`）+ 重启服务。

```sh
cd /var/www/xianyu && git pull --ff-only fork main
sudo systemctl restart xianyu-api xianyu-worker
sudo journalctl -u xianyu-api -u xianyu-worker --since "2 min ago" --no-pager | tail -30
```

本次**非代码操作**（SQL）：把 goods 17 / 25 的付款后发货配置重新置为启用
（10-26 11:37 被保存动作写成 `enabled=0`，代码无法自动纠偏）。

```sql
UPDATE delivery_goods_config
SET config_json = JSON_SET(config_json, '$.payDelivery.enabled', 1),
    updated_time = NOW()
WHERE goods_id IN (17, 25) AND deleted = 0;
```

验证：

```sql
SELECT id, goods_id,
       JSON_EXTRACT(config_json,'$.payDelivery.enabled') AS en,
       JSON_EXTRACT(config_json,'$.payDelivery.mode') AS mode,
       JSON_EXTRACT(config_json,'$.payDelivery.cardGroupId') AS cg,
       updated_time
FROM delivery_goods_config WHERE goods_id IN (17, 25);
-- 期望：en=1, mode=card, cg=4
```

规则复算（服务器上只读脚本）：`resolve_realtime_delivery_rule(2, '1091821311650')`
→ `delivery_mode=card, card_group_id=4, delivery_timing=after_payment`；
`1094823097374` 同样命中。`1092280824859` / `1092539311888` / `1092046159577` /
`1094895404770` 仍然 **NO RULE**（从未保存过发货配置，需人工在页面配置）。

补发通道干跑（worker 每 600s 自动执行，等效）：
`chatScanned=1 / chatSkipped=1 / chatSkippedHumanReply=6 / chatRecovered=0`
—— 6 条因"付款后 6h 内已有卖家 OUT 消息（人工已手工发 key）"被安全阀挡下，
1 条（`xianyu_chat_message.id=766`，goods `1094895404770` 买家 `68242230133`）因无配置跳过。

## d8220ae（2026-10-29 00:18 +07）自动发货 WS 游标 / 订单兜底修复

代码发布：服务器 `git pull --ff-only fork main` + `systemctl restart xianyu-api xianyu-worker`。

非代码项（必须执行，否则兜底仍要等 10 分钟）：

```bash
cd /var/www/xianyu
cp .env /tmp/.env.bak.$(date +%s)
sed -i 's/^DELIVERY_RECOVERY_INTERVAL_SECONDS=600$/DELIVERY_RECOVERY_INTERVAL_SECONDS=60/; \
        s/^DELIVERY_RECOVERY_MIN_AGE_SECONDS=300$/DELIVERY_RECOVERY_MIN_AGE_SECONDS=60/' .env
systemctl restart xianyu-api xianyu-worker
```

数据修复（本次一次性，可复现）：

```sql
-- 1) acc3 订单从没同步过：手工触发一次全量同步（页面「同步已售订单」等效）
--    POST /api/order/syncSoldOrders {"xianyu_account_id":3}  → inserted 12
-- 2) 两条 use synthetic session_id 的 unknown attempt 重置为可重试（卡密已被
--    heal 回池，未真正发出），等真实会话建立后由补发通道重发
UPDATE realtime_delivery_attempt
   SET state='failed', retry_safe=1, last_error_code='synthetic_session_id',
       error_message='会话未建立，已重置等待真实会话', updated_time=NOW()
 WHERE id IN (45,46);
```

新增/受影响 .env 项：`DELIVERY_RECOVERY_INTERVAL_SECONDS=60`、`DELIVERY_RECOVERY_MIN_AGE_SECONDS=60`；
代码新增配置 `DELIVERY_RECOVERY_ORDER_SYNC_ENABLED`（默认 true）、
`AI_REPLY_MAX_MESSAGE_AGE_SECONDS`（默认 900，均为可选覆盖）。
