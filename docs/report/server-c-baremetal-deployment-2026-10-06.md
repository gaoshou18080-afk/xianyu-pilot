# 服务器 C 裸机正式发布报告

日期：2026-10-06

目标：C / `104.234.174.250`

目录：`/var/www/xianyu`

代码版本：`12c6644`

方案：裸机，无 Docker

## 发布结果

| 项目 | 结果 |
| --- | --- |
| Node.js | `22.23.1` |
| MySQL 数据库与迁移 | `xianyu_opensource`，45 个迁移全部成功 |
| API | active，`/health/ready` 全部组件 `ok` |
| Worker | active，心跳文件新鲜，`--check` 返回 0 |
| Crawler | active，`/ready` 返回 200，浏览器容量为 1 |
| Redis | 独立实例 `127.0.0.1:16379`，active |
| Nginx | 配置检查通过，站点已加载 |
| TLS | DNS-01 证书签发成功，80→443 跳转通过，续期演练通过 |
| Web | 首页经 C 的 Nginx 返回 200 |
| 登录 | `admin/admin123` 经域名请求返回 200，token 有效 |
| Chromium 冒烟 | 真实启动 `149.0.7827.55`，打开测试页并正常退出 |
| 公网 DNS | 1.1.1.1、8.8.8.8 与 Cloudflare 权威 NS 均返回 `104.234.174.250` |
| 公网入口 | HTTP 返回 301，HTTPS HTTP/2 返回 200，`/readyz` 为 ready |

证书：`CN=xianyu.enjoygoo.win`，签发机构 `Let's Encrypt YE1`，有效期
`2026-10-06` 至 `2027-01-04`。

## 资源数据

- 服务器总内存：约 3915MB。
- Swap：0。
- 浏览器启动期间可用内存：约 1675MB。
- 浏览器关闭后可用内存：约 1801MB。
- API cgroup：约 104MB。
- Worker cgroup：约 73MB。
- Crawler Node 空载：约 86-115MB。
- Redis：约 4MB。

## 发布期发现并修复

API 和 Worker 原先都启用了 systemd `PrivateTmp=true`，会使用不同的 `/tmp`
命名空间。Worker 的 `/tmp/xianyu-scheduler-heartbeat` 可能无法被 API 或人工
检查读取。发布时将这两个服务的 `PrivateTmp` 移除，重启后
`python -m app.worker --check` 返回 0。

## 公网验收

DNS 切换后，公共解析均指向服务器 C：

```text
1.1.1.1 -> 104.234.174.250
8.8.8.8 -> 104.234.174.250
Cloudflare 权威 NS -> 104.234.174.250
```

从开发机绕过本机 hosts 直连服务器 C 验证：

```text
http://xianyu.enjoygoo.win  -> 301
https://xianyu.enjoygoo.win/healthz -> HTTP/2 200
https://xianyu.enjoygoo.win/readyz  -> {"status":"ready"}
```

## 剩余项

1. 首次登录后应立即修改默认管理员密码 `admin123`。
2. `gaoshou18080-afk` 对 `dameng2026/xianyu-pilot` 没有写权限，本地部署提交
   尚未推送到远端；需要仓库授权、改用有权限的远端或确认 fork。
3. 仓库缺少全局约定要求的 `docs/important.md`，需要由项目负责人恢复或确认
   无遗漏决策。
