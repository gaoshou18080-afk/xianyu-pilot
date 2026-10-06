# 服务器 C 裸机运维手册

## 基本信息

- 域名：`xianyu.enjoygoo.win`
- 服务器：C / `104.234.174.250` / 新加坡
- SSH：`ssh -i /var/www/sshkey/xinjiapo guanli@104.234.174.250`
- 部署目录：`/var/www/xianyu`
- 运行方式：裸机，不使用 Docker
- Web：Nginx 托管 `/var/www/xianyu/apps/web/dist`
- API：`127.0.0.1:15177`
- Crawler：`127.0.0.1:15178`
- HTTPS：公网 `443`，由 Nginx 终止 TLS
- Redis：`127.0.0.1:16379`，systemd 服务 `xianyu-redis`
- MySQL：本机 `127.0.0.1:3306`，数据库 `xianyu_opensource`

## 服务与文件

| 项目 | 位置 |
| --- | --- |
| API systemd | `/etc/systemd/system/xianyu-api.service` |
| Worker systemd | `/etc/systemd/system/xianyu-worker.service` |
| Crawler systemd | `/etc/systemd/system/xianyu-crawler.service` |
| Redis systemd | `/etc/systemd/system/xianyu-redis.service` |
| Redis 配置 | `/etc/redis/xianyu.conf` |
| Nginx 站点 | `/etc/nginx/sites-enabled/xianyu.enjoygoo.win` |
| 环境变量 | `/var/www/xianyu/.env` |
| secrets | `/var/www/xianyu/secrets/` |
| Cloudflare DNS 凭据 | `/etc/letsencrypt/cloudflare.ini`，`0600 root:root` |
| Python venv | `/var/www/xianyu/.venv/` |
| Node.js | `/opt/node22/bin/node` |
| Playwright Chromium | `/home/guanli/.cache/ms-playwright` |
| Scheduler 心跳 | `/tmp/xianyu-scheduler-heartbeat` |

API 与 Worker 不使用 `PrivateTmp`，二者必须共享同一个系统 `/tmp`，否则 Worker
写出的调度器心跳无法被其他进程读取。

## 状态检查

```sh
ssh -i /var/www/sshkey/xinjiapo guanli@104.234.174.250
free -m
systemctl status --no-pager xianyu-redis xianyu-api xianyu-worker xianyu-crawler nginx
ss -ltnp | grep -E '15177|15178|16379|:80 |:443 '
curl -fsS http://127.0.0.1:15177/health/ready
curl -fsS http://127.0.0.1:15178/ready
cd /var/www/xianyu/apps/api
../../.venv/bin/python -m app.worker --check
```

边缘健康检查：

```sh
curl --resolve xianyu.enjoygoo.win:80:104.234.174.250 \
  http://xianyu.enjoygoo.win/healthz
curl --resolve xianyu.enjoygoo.win:80:104.234.174.250 \
  http://xianyu.enjoygoo.win/readyz
```

## 日志与重启

```sh
journalctl -u xianyu-api -n 100 --no-pager
journalctl -u xianyu-worker -n 100 --no-pager
journalctl -u xianyu-crawler -n 100 --no-pager
journalctl -u xianyu-redis -n 100 --no-pager

sudo systemctl restart xianyu-api xianyu-worker xianyu-crawler
sudo systemctl reload nginx
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
4. 服务器只拉取代码，不执行 Vite/TypeScript 构建：

```sh
cd /var/www/xianyu
git pull --ff-only
```

5. 将本地 `dist` 发布包上传到对应目录并重启服务：

```sh
scp -i /var/www/sshkey/xinjiapo -r apps/web/dist/* \
  guanli@104.234.174.250:/var/www/xianyu/apps/web/dist/
scp -i /var/www/sshkey/xinjiapo -r apps/crawler/dist/* \
  guanli@104.234.174.250:/var/www/xianyu/apps/crawler/dist/
ssh -i /var/www/sshkey/xinjiapo guanli@104.234.174.250 \
  'sudo systemctl restart xianyu-api xianyu-worker xianyu-crawler'
```

6. 依次验证 API、Worker 心跳、Crawler、Nginx 和登录接口。
7. 有 SQL、目录、权限、systemd、Nginx、Redis 等非代码变化时，登记
   `docs/deploy.md`。

## 内存控制

当前服务器物理内存约 3.9GB，Swap 为 0。Crawler 固定限制：

```dotenv
CRAWLER_MAX_CONCURRENCY=1
CRAWLER_MAX_SESSIONS=10
CRAWLER_FORCE_HEADLESS=true
```

不要在未评估内存的情况下提高并发。真实 Chromium 单次操作应保留至少
500-800MB 余量；如果生产流量增加，优先扩容物理内存并配置 2-4GB Swap。

## DNS 与 TLS

DNS 正常指向：

```sh
dig +short @1.1.1.1 xianyu.enjoygoo.win A
# 应返回 104.234.174.250
```

服务器 C 已使用 Cloudflare DNS-01 提前签发
`/etc/letsencrypt/live/xianyu.enjoygoo.win/` 证书。A 记录已切换到
`104.234.174.250`，可用以下命令复查公网：

```sh
curl -I http://xianyu.enjoygoo.win
curl -I https://xianyu.enjoygoo.win
```

检查 80 到 443 跳转、证书域名和自动续期：

```sh
echo | openssl s_client -connect 104.234.174.250:443 \
  -servername xianyu.enjoygoo.win 2>/dev/null |
  openssl x509 -noout -subject -issuer -dates -ext subjectAltName
sudo certbot renew --cert-name xianyu.enjoygoo.win --dry-run
```

证书续期使用 `/etc/letsencrypt/cloudflare.ini`，不依赖 A 记录或 HTTP-01。

## 回滚

```sh
cd /var/www/xianyu
git switch --detach <上一个正常版本>
sudo systemctl restart xianyu-api xianyu-worker xianyu-crawler
```

如果版本包含数据库迁移，回滚代码前必须确认迁移可逆或数据库向后兼容。
