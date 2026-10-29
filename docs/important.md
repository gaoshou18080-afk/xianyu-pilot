# 重要决策（docs/important.md）

> 本文件优先于旧任务清单、旧 API 盘点、旧待开发清单和历史验收描述。

## 当前生产拓扑（2026-10-29）

- `xianyu.enjoygoo.win` 当前生产节点为服务器 W：`104.234.177.211`。
- 部署目录：`/var/www/xianyu`；使用独立 systemd 单元 `xianyu-api`、
  `xianyu-worker`、`xianyu-crawler`、`xianyu-redis`。
- W 同时承载其他业务，任何发布只允许 reload Nginx，不得重启或停止共用
  Nginx/MySQL/PHP，也不得改动其他站点。
- 服务器 C `104.234.174.250` 上的本项目代码和数据保留作回滚，但 xianyu
  服务已停止并取消开机，xianyu Nginx 站点已从 `sites-enabled` 移除。
- 服务器 Y `47.114.95.251` 当前没有本项目部署。**禁止在未确认账号和数据库
  隔离方案前把 Y 作为第二个活动节点**；同一批闲鱼账号双活会形成重复 WS
  消费、重复发货和账号互踢风险。

## 运行约束

- 本项目同一时间只允许一个活动 Worker/Crawler 集合消费同一批闲鱼账号。
- Y 若要接管生产，必须先停止 W 的 Worker/Crawler，并明确数据库是切换还是
  独立实例；若要双活，必须使用不同账号和独立数据库，且单独评审。
- 当前迁移、发布和回滚步骤见 `docs/deploy.md` 2026-10-29 条目。
