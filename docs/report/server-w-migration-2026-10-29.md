# 服务器 C 到 W 完整迁移报告

日期：2026-10-29

> 历史说明：用户随后纠正目标，最终生产已迁到服务器 Y。本报告中的 W
> 生产状态已被 `server-y-migration-2026-10-29.md` 取代，W 闲鱼资源已清理。

## 结果

项目 `xianyu-pilot` 已从服务器 C 完整迁移到服务器 W，公网域名已切换，
W 的 API、Worker、Crawler、Redis 和 Nginx 站点均运行正常。服务器 C 上本
项目的四个 systemd 单元已停止并取消开机，xianyu Nginx 站点已移除；C 的
Nginx、PHP 7.4、PHP 8.4 和其他业务未停止。

| 项目 | 源 C | 目标 W |
| --- | --- | --- |
| IP | `104.234.174.250` | `104.234.177.211` |
| 代码版本 | `6211b28` | `6211b28` |
| 目录 | `/var/www/xianyu` | `/var/www/xianyu` |
| 数据库 | `xianyu_opensource`，73 表 / 45 迁移 | 完整导入，73 表 / 45 迁移 |
| Redis | 独立 `127.0.0.1:16379` | 独立 `127.0.0.1:16379` |
| Web/API/Crawler | 迁移后被停用 | 已启用 |

## 迁移数据

- 复制项目文件、`.env`、`secrets/`、上传文件、Web `dist/`、Crawler `dist/`
  和 Git 仓库；Python venv、Node 依赖和 Playwright 浏览器在 W 重新安装。
- 停写后执行 MySQL 逻辑全库导出/导入，并迁移 Redis RDB/AOF 持久化目录。
- W 使用独立数据库账号和独立 Redis 端口，没有修改 W 上其他业务的数据。
- W 已安装 Node.js 22.23.1、Python 3.12 venv、Playwright 1.61.1 /
  Chromium 1228，并通过真实浏览器启动测试。

## 验收

- `xianyu-api`、`xianyu-worker`、`xianyu-crawler`、`xianyu-redis`：active/running。
- API readiness：`{"status":"ready"}`，数据库、迁移、消息自动化、Token 刷新器、
  Redis 组件均为 `ok`。
- Crawler `/ready`：正常；Worker `--check`：退出码 0。
- W 本机通过 IP + SNI 访问 HTTPS 首页返回 200，`/readyz` 返回 ready。
- Cloudflare A 记录已更新为 `104.234.177.211`，TTL 60，DNS-only。
- W 证书独立通过 Cloudflare DNS-01 签发，有效期至 2027-01-27。
- W 上现有商城站点 `shop.enjoygoo.win` 直接回源仍返回 200。

## 已知风险

- W 首次启动后账号 1 报告 `SESSION_EXPIRED`，账号 2、3 已连接。账号 1
  需要在业务界面重新登录并完成滑块/授权；这是切换出口 IP 后触发的会话
  过期，不是数据缺失。
- 服务器 Y 当前为空。未在 Y 上启动本项目，因为复制同一数据库并同时运行
  Worker/Crawler 会造成同账号双活和重复发货风险。Y 的拓扑需单独确认。

## 回滚

C 保留完整项目和停写快照。若 W 出现不可恢复问题，将 Cloudflare A 记录改回
`104.234.174.250`，恢复 C 的 Nginx 站点链接并启动四个 xianyu 单元。回滚前
必须先停止 W 的 Worker/Crawler，不能形成双活。
