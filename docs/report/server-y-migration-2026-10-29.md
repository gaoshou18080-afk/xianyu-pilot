# 服务器 W 到 Y 完整迁移报告

日期：2026-10-29

## 结果

项目 `xianyu-pilot` 已从误切换的服务器 W `104.234.177.211` 完整迁到服务器
Y `47.114.95.251`。Cloudflare 域名已切换到 Y，Y 的 API、Worker、Crawler、
Redis 和 Nginx 站点运行正常；W 的闲鱼专用资源已全部清理，W 上的其他项目未停止
或修改。

| 项目 | 源 W | 目标 Y |
| --- | --- | --- |
| IP | `104.234.177.211` | `47.114.95.251` |
| 代码版本 | `8fc2fc8` | `8fc2fc8` |
| 目录 | `/var/www/xianyu` | `/var/www/xianyu` |
| 数据库 | `xianyu_opensource`，73 表 / 45 迁移 | 完整导入，73 表 / 45 迁移 |
| Redis | 独立 `127.0.0.1:16379` | 独立 `127.0.0.1:16379` |
| Web/API/Crawler | 已全部清理 | 已启用 |

## 数据与运行时

- 复制项目文件、`.env`、`secrets/`、上传文件、Web `dist/`、Crawler `dist/`
  和 Git 仓库。
- MySQL 导入 73 张表、45 个迁移；聊天消息 1552、订单 298、发货尝试 54、
  卡密 212，与切换源一致。
- Redis 保留迁移键和 TTL，Y 原有 6379 Redis 未修改。
- Y 安装 Node 22.23.1、Python 3.12.16、项目 venv、Crawler 依赖和
  Playwright Chromium；真实 Chromium 页面加载测试通过。
- 恢复 TLS live/archive/renewal/accounts 和 Cloudflare 凭据，安装
  certbot + DNS-Cloudflare 插件，DNS-01 续期 dry-run 通过。

## 验收

- `xianyu-redis`、`xianyu-api`、`xianyu-worker`、`xianyu-crawler`：active/enabled。
- API `/health/edge-ready`：`{"status":"ready"}`。
- Crawler `/health`：`{"status":"ok","service":"xianyu-crawler"}`。
- Worker check 退出码 0，调度器和发货恢复循环均启动。
- HTTP 公网 301 到 HTTPS；首页、静态资源、`/healthz`、`/readyz` 和登录接口
  状态符合预期。
- Cloudflare、Google、阿里 DNS 均返回 `47.114.95.251`。
- Cloudflare A 记录 TTL 60、DNS-only。
- 证书 CN `xianyu.enjoygoo.win`，有效期至 2027-01-27，自动续期 timer active。

## W 清理

- 删除四个 `xianyu-*` systemd 单元及其启用链接。
- 移除 xianyu Nginx `sites-enabled`/`sites-available` 配置并 reload Nginx。
- 删除 `/var/www/xianyu`、`/var/lib/redis-xianyu`、Redis 配置和密码。
- 删除 `xianyu.enjoygoo.win` 证书及 renewal/archive/live 数据。
- 删除数据库 `xianyu_opensource` 和专用 MySQL 用户。
- 清理后 W 无闲鱼进程、端口、目录、数据库和站点残留。
- W 的 Nginx、MySQL、PHP 8.4 始终 active；`shop.enjoygoo.win`、
  `pdd.enjoygoo.win` 复验均为 200。

## 已知风险

- 账号 1 因出口 IP 改变报告 `SESSION_EXPIRED`，需要在 Y 的页面重新登录并完成
  滑块/授权；这是平台会话失效，不是迁移缺失。
- Y 物理内存约 1.7GB，已配置 2GB Swap。Crawler 必须保持
  `CRAWLER_MAX_CONCURRENCY=1`，不建议在未扩容时提高并发。
- Playwright 本地备份包的清单 SHA-256 与实际包不一致，但 Y 上浏览器真实启动
  测试通过；完整回滚前应重新验证该备份包。

## 回滚

完整迁移快照保存在
`/var/www/vm_share/yuenan/xianyu-w-to-y-20261029/`。回滚前必须先停止 Y 的
Worker/Crawler，再在经审批的独立节点恢复。禁止恢复 W 与 Y 同账号双活。
