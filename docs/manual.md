# 服务器 Y 裸机运维手册

## 基本信息

- 域名：`xianyu.enjoygoo.win`
- 服务器：Y / `47.114.95.251` / 阿里云杭州
- SSH：`ssh root@47.114.95.251`
- 系统：Ubuntu 22.04.5 LTS
- 部署目录：`/var/www/xianyu`
- 运行方式：裸机，不使用 Docker
- Web：Nginx 托管 `/var/www/xianyu/apps/web/dist`
- API：`127.0.0.1:15177`
- Crawler：`127.0.0.1:15178`
- HTTPS：公网 `443`，由 Nginx 终止 TLS
- Redis：`127.0.0.1:16379`，systemd 服务 `xianyu-redis`
- MySQL：本机 `127.0.0.1:3306`，数据库 `xianyu_opensource`

Y 还承载其他业务。发布只允许 reload Nginx，不得重启或停止共用
Nginx/MySQL/PHP/Redis，也不得修改其他站点。

## 服务与文件

| 项目 | 位置 |
| --- | --- |
| API systemd | `/etc/systemd/system/xianyu-api.service` |
| Worker systemd | `/etc/systemd/system/xianyu-worker.service` |
| Crawler systemd | `/etc/systemd/system/xianyu-crawler.service` |
| Redis systemd | `/etc/systemd/system/xianyu-redis.service` |
| Redis 配置 | `/etc/redis/xianyu.conf` |
| Redis 密码 | `/etc/redis/xianyu-secret.conf` |
| Nginx 站点 | `/etc/nginx/sites-enabled/xianyu.enjoygoo.win` |
| 环境变量 | `/var/www/xianyu/.env` |
| secrets | `/var/www/xianyu/secrets/` |
| Cloudflare DNS 凭据 | `/etc/letsencrypt/cloudflare.ini`，`0600 root:root` |
| Python | `/opt/python3.12`，项目 venv 为 `/var/www/xianyu/.venv/` |
| Node.js | `/opt/node22/bin/node` |
| Playwright Chromium | `/home/guanli/.cache/ms-playwright` |
| Scheduler 心跳 | `/tmp/xianyu-scheduler-heartbeat` |

API 与 Worker 不使用 `PrivateTmp`，二者必须共享同一个系统 `/tmp`，否则 Worker
写出的调度器心跳无法被其他进程读取。

MySQL 应用连接虽然配置为 `127.0.0.1`，服务端实际按
`xianyu_app@localhost` 匹配。该账号只应拥有 `xianyu_opensource.*` 的
`SELECT/INSERT/UPDATE/DELETE/CREATE/ALTER/INDEX`。

## 状态检查

```sh
ssh root@47.114.95.251
free -h
swapon --show
systemctl status --no-pager \
  xianyu-redis xianyu-api xianyu-worker xianyu-crawler nginx mysql php8.4-fpm
ss -ltnp | grep -E '15177|15178|16379|:80 |:443 '
curl -fsS http://127.0.0.1:15177/health/edge-ready
curl -fsS http://127.0.0.1:15178/health
cd /var/www/xianyu/apps/api
../../.venv/bin/python -m app.worker --check
```

公网检查应从非 Y 的机器执行，或使用固定回源：

```sh
curl -I --resolve xianyu.enjoygoo.win:80:47.114.95.251 \
  http://xianyu.enjoygoo.win/
curl -fsS --resolve xianyu.enjoygoo.win:443:47.114.95.251 \
  https://xianyu.enjoygoo.win/readyz
```

## 日志与重启

```sh
journalctl -u xianyu-api -n 100 --no-pager
journalctl -u xianyu-worker -n 100 --no-pager
journalctl -u xianyu-crawler -n 100 --no-pager
journalctl -u xianyu-redis -n 100 --no-pager

systemctl restart xianyu-api xianyu-worker xianyu-crawler
systemctl reload nginx
```

正常 Worker 日志应包含：

```text
scheduler worker started interval=15s batch=20 timeout=300s
delivery_recovery loop started interval=15s batch=50 minAge=10s
```

## 发布流程

1. 本地完成开发、测试并执行 `git commit`、`git push`。
2. 本地构建 Web 与 Crawler：

```sh
cd /var/www/xianyu2
npm --prefix apps/web run build
npm --prefix apps/crawler run build
```

3. 发布前检查 `docs/deploy.md` 中是否还有目标版本的非代码操作。
4. 服务器只拉取代码，不执行 Vite/TypeScript 构建，也不在服务器提交代码：

```sh
ssh root@47.114.95.251
sudo -u guanli git -C /var/www/xianyu pull --ff-only fork main
```

5. 将本地 `dist` 发布包上传到 Y，再重启闲鱼服务：

```sh
scp -r apps/web/dist/* \
  root@47.114.95.251:/var/www/xianyu/apps/web/dist/
scp -r apps/crawler/dist/* \
  root@47.114.95.251:/var/www/xianyu/apps/crawler/dist/
ssh root@47.114.95.251 \
  'systemctl restart xianyu-api xianyu-worker xianyu-crawler'
```

6. 依次验证 API、Worker 心跳、Crawler、Nginx、公网 readiness 和登录接口。
7. 有 SQL、目录、权限、systemd、Nginx、Redis、证书等非代码变化时，登记
   `docs/deploy.md`。

## 内存控制

当前物理内存约 1.7GB，另有 2GB Swap。Crawler 固定限制：

```dotenv
CRAWLER_MAX_CONCURRENCY=1
CRAWLER_MAX_SESSIONS=10
CRAWLER_FORCE_HEADLESS=true
```

不要在未评估内存的情况下提高并发。真实 Chromium 单次操作应保留至少
500-800MB 余量；Swap 只用于短时兜底，不能替代物理内存扩容。

## DNS 与 TLS

DNS 正常指向：

```sh
dig +short @1.1.1.1 xianyu.enjoygoo.win A
# 应返回 47.114.95.251
```

Cloudflare A 记录为 TTL 60、DNS-only。阿里云安全组必须放行 TCP 80/443。

检查证书和自动续期：

```sh
certbot certificates --cert-name xianyu.enjoygoo.win
systemctl status --no-pager certbot.timer
certbot renew --cert-name xianyu.enjoygoo.win \
  --dry-run --non-interactive --no-random-sleep-on-renew
```

续期使用 `/etc/letsencrypt/cloudflare.ini` 和 `/etc/letsencrypt/accounts/`，
不依赖 A 记录或 HTTP-01。迁移或恢复证书时必须同时保留 live、archive、
renewal、accounts 和 Cloudflare 凭据。

## 回滚

```sh
cd /var/www/xianyu
sudo -u guanli git switch --detach <上一个正常版本>
systemctl restart xianyu-api xianyu-worker xianyu-crawler
```

如果版本包含数据库迁移，回滚代码前必须确认迁移可逆或数据库向后兼容。
完整迁移快照保存在
`/var/www/vm_share/yuenan/xianyu-w-to-y-20261029/`。任何回滚节点启动前必须先
停止 Y 的 Worker/Crawler，禁止同账号双活。
