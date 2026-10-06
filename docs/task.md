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

## 待办 / 说明

- 当前 Web 为 Vite dev server（`npm run dev`），生产如需静态资源可 `npm --prefix apps/web run build` 后由 nginx 托管。
- `runtime-status` 中 `commercialBridgeMode=commercial` 且探活失败（约 10s 超时），因商业版桥接未配置 token；不影响本地功能，如需纯本地兜底可在 `.env` 关闭商业版桥接相关配置。
- 开发机本地脚本未配置 systemd 自启；服务器 C 已启用 systemd 自启。
- 服务器 C 裸机发布评估见 `docs/report/server-c-baremetal-preflight-2026-10-05.md`：当前可用内存约 399MB、Swap 为 0，最低需 4GB Swap 和 1.2GB 可用物理内存并限制 Crawler 并发为 1。
- DNS 已切换到 `104.234.174.250`；Cloudflare 权威 NS、1.1.1.1 和 8.8.8.8
  公共解析均已确认。
- `gaoshou18080-afk` 当前对 `dameng2026/xianyu-pilot` 没有写权限，服务器 C 部署
  配置提交尚未推送；需要仓库授权、改用有权限的远端，或确认后推送到 fork。
- 服务器 C 的详细运维命令见 `docs/manual.md`，本次发布记录见
  `docs/deploy.md`，上线结果见
  `docs/report/server-c-baremetal-deployment-2026-10-06.md`。
- 仓库缺少全局约定要求的 `docs/important.md`，正式部署前需恢复或确认无遗漏决策。
