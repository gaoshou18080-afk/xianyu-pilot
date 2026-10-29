# 重要决策（docs/important.md）

> 本文件优先于旧任务清单、旧 API 盘点、旧待开发清单和历史验收描述。

## 当前生产拓扑（2026-10-29）

- `xianyu.enjoygoo.win` 当前生产节点为服务器 Y：`47.114.95.251`。
- 部署目录：`/var/www/xianyu`；使用独立 systemd 单元 `xianyu-api`、
  `xianyu-worker`、`xianyu-crawler`、`xianyu-redis`。
- Y 同时承载其他业务，任何发布只允许 reload Nginx，不得重启或停止共用
  Nginx/MySQL/PHP/Redis，也不得改动其他站点。
- 服务器 C `104.234.174.250` 上的本项目代码和数据保留作回滚，但 xianyu
  服务已停止并取消开机，xianyu Nginx 站点已从 `sites-enabled` 移除。
- 服务器 W `104.234.177.211` 上误切换产生的闲鱼代码、数据库、Redis、证书、
  Nginx 站点和 systemd 单元已全部清理；W 的共用 Nginx/MySQL/PHP 与其他
  站点保持运行。
- Cloudflare A 记录当前指向 Y，TTL 60，DNS-only；源站安全组必须放行
  TCP 80/443。

## 运行约束

- 本项目同一时间只允许一个活动 Worker/Crawler 集合消费同一批闲鱼账号。
- 禁止把 W、C 或新的节点作为同账号双活实例；双活会形成重复 WS 消费、重复
  发货和账号互踢风险。
- Y 应用通过 `127.0.0.1` 连接 MySQL，但 MySQL 将连接识别为
  `xianyu_app@localhost`。应用账号必须保留
  `SELECT/INSERT/UPDATE/DELETE/CREATE/ALTER/INDEX` 权限，仅限
  `xianyu_opensource.*`。
- HTTPS 续期依赖 `/etc/letsencrypt/accounts/`、续期配置和
  `/etc/letsencrypt/cloudflare.ini`，三者必须同时存在。
- 当前迁移、发布和回滚步骤见 `docs/deploy.md` 2026-10-29 条目。
