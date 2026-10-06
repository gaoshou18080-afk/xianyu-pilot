# 服务器 C 裸机发布前评估

日期：2026-10-05

目标服务器：C / `104.234.174.250` / `sg-caipiao`

目标目录：`/var/www/xianyu`

代码版本：`12c6644`

方案：裸机，非 Docker

## 结论

裸机方案可以作为正式方案，不需要 Docker，但 **服务器 C 当前不能直接安全上线 Crawler**。

主要阻塞项：

1. 当前总内存约 3.9GB，可用内存约 399MB，Swap 为 0。
2. 项目 API、Worker、Crawler 空载预计约 340MB，Crawler 启动真实 Chromium 操作预计再占 500-800MB。
3. 服务器已有 MySQL、PHP 队列和另一个项目的 Chrome，当前没有足够内存容纳本项目浏览器。
4. 服务器只有 Node.js 18.19.1 / npm 9.2.0，不满足项目要求的 Node.js 22.23.1 / npm 10.9.8。
5. DNS 尚未切换，`1.1.1.1` 和 `8.8.8.8` 仍返回旧地址 `166.88.227.132`。

最低上线条件：

- 至少增加 4GB Swap，并在启动前保证 1.2GB 以上可用物理内存。
- `CRAWLER_MAX_CONCURRENCY=1`，禁止两个浏览器并发操作。
- 尽量停止或迁移服务器上另一个项目的 Chrome，并压缩非必要 PHP 队列。
- 首次上线先启动 API、Worker、Web，Crawler 单独验证，不能随整套服务直接放开。

推荐配置：

- 物理内存升级到 8GB。
- 保留 2-4GB Swap。
- Crawler 并发固定为 1。
- Web 静态文件由 Nginx 托管，不运行 Vite dev server。

## 目标架构

```text
公网 80/443
  -> Nginx 1.24
       -> /                 /var/www/xianyu/apps/web/dist
       -> /api/*            127.0.0.1:15177
       -> /uploads/*        127.0.0.1:15177

systemd
  -> xianyu-api          uvicorn app.main:app，127.0.0.1:15177
  -> xianyu-worker       python -m app.worker
  -> xianyu-crawler      node dist/server.js，127.0.0.1:15178

复用服务器现有服务
  -> MySQL 8.0.46
  -> Redis 7.0.15
  -> Nginx 1.24.0
```

不部署：

- Docker Engine / Docker Compose。
- 项目专用 MySQL、Redis、Nginx。
- Vite 开发服务器。

## 当前服务器基线

| 项目 | 当前状态 | 评估 |
| --- | --- | --- |
| 操作系统 | Ubuntu 24.04、x86_64、4 CPU | 可用 |
| 内存 | 约 3915MB；已用约 3515MB；可用约 399MB | 阻塞 |
| Swap | 0 | 阻塞 |
| 磁盘 | 43GB；已用 13GB；可用 31GB | 充足 |
| Python | 3.12.3 | 可用 |
| Node | 18.19.1 | 不满足 |
| npm | 9.2.0 | 不满足 |
| MySQL | 8.0.46 | 可复用 |
| Redis | 7.0.15 | 可复用 |
| Nginx | 1.24.0，80/443 已监听 | 可复用 |
| 端口 | 15176/15177/15178/8080 空闲 | 可用 |
| 应用目录 | `/var/www/xianyu` 不存在 | 待创建 |
| 应用站点配置 | 未发现 | 待创建 |
| TLS 证书 | 未发现目标域名证书 | 待申请 |

只读采样时主要内存占用：

| 进程 | RSS |
| --- | ---: |
| MySQL | 约 2089MB |
| PHP/Supervisor 队列 | 约 865-1171MB |
| 另一个项目的 Chrome | 约 592-614MB |
| Nginx | 约 94MB |
| Node | 约 76MB |

RSS 包含共享页，不能简单逐项相加；这里用于判断容量风险，不能作为精确内存核算。

## 本项目内存预算

本机实测空载 RSS：

| 进程 | 实测 RSS |
| --- | ---: |
| API | 约 125MB |
| Scheduler Worker | 约 78MB |
| Crawler Node | 约 137MB |
| Web 静态资源 | 基本为 0，复用 Nginx |
| 小计 | 约 340MB |

Chromium 实测：

| 场景 | 内存 |
| --- | ---: |
| 单个浏览器空启动 | 约 297MB |
| 加载一个简单页面 | 约 496MB |
| 两个浏览器加载两个页面 | 约 1013MB |
| 关闭浏览器后 | 进程完整释放 |

真实闲鱼页面包含脚本、图片和登录态，单次操作按 500-800MB 预留。

因此：

- 项目非浏览器进程约需 340MB。
- 单次 Crawler 操作总计约需 840-1140MB。
- 当前可用 399MB，直接启动 Crawler 很可能触发 OOM。
- 两个并发浏览器基本不可行。

## 裸机与 Docker

Docker 不是发布必需品。裸机可以复用现有 MySQL、Redis 和 Nginx，通常能少掉 Docker daemon、额外 MySQL/Redis 容器和一部分重复缓存。

但裸机节省的内存主要属于基础服务，量级通常为几百 MB；真正占内存的是 Chromium。因此：

- 裸机能降低内存占用，但不能替代扩容。
- Docker 的优势是依赖隔离、版本固定、部署一致性和回滚简单。
- 裸机的代价是 Node、Python、Playwright、系统库和 systemd 都由宿主机维护。
- 在服务器 C 已有 MySQL/Redis/Nginx 的前提下，裸机是合理选择。

## 本地构建、服务器直接使用

可以，但发布物不是单个可执行文件，服务器仍需具备运行时依赖。

本地负责：

```sh
npm --prefix apps/web ci
npm --prefix apps/web run build
npm --prefix apps/crawler ci
npm --prefix apps/crawler run build
python -m compileall -q apps/api/app
```

产生：

- 前端静态文件：`apps/web/dist/`
- Crawler JavaScript：`apps/crawler/dist/`

服务器负责：

- 拉取已提交并推送的代码版本。
- 接收本地构建的 `apps/web/dist` 和 `apps/crawler/dist`。
- 安装/固定 Node.js 22.23.1 和 npm 10.9.8。
- 安装 Crawler 生产依赖。
- 安装 Playwright Chromium 1.61.1 对应浏览器。
- 创建 Python venv 并安装 `apps/api/requirements.txt`。
- 运行数据库迁移。
- 启动 systemd 服务并配置 Nginx。

项目 `.gitignore` 已忽略 `dist/`，所以有两种安全做法：

1. 推荐：代码从 Git 拉取，单独把本地构建产物作为发布包传到服务器。
2. 不推荐：把 `dist/` 强制提交到 Git；这会让仓库产生大量构建噪音。

服务器不允许执行 Vite/TypeScript 构建。安装 Python/npm 生产依赖和 Playwright 浏览器属于运行时安装，不属于源码构建。

## 首次发布步骤

1. 确认 DNS 切换到 `104.234.174.250`。
2. 清理本地未提交内容，提交并推送代码。
3. 备份 C 上现有 Nginx、MySQL 和应用目录状态。
4. 安装 Node.js 22.23.1 / npm 10.9.8。
5. 创建 `/var/www/xianyu` 并从 Git 拉取指定版本。
6. 上传本地构建的 Web/Crawler `dist`。
7. 创建 `.env` 和随机 secrets，生产值不能复用开发机密钥。
8. 创建 MySQL 数据库和最小权限用户。
9. 运行 45 个迁移并检查 schema 当前。
10. 安装 API venv、Crawler 生产依赖和 Playwright Chromium。
11. 先启动 API 和 Worker，验证健康检查。
12. 启动 Crawler 空载，验证 `/ready`。
13. 用低风险操作验证单次浏览器内存峰值。
14. 配置 systemd 和 Nginx。
15. 申请 TLS 证书，验证 HTTP 跳转和 HTTPS。
16. 登录默认管理员后立即更换密码。

建议健康检查：

```sh
curl -fsS http://127.0.0.1:15177/health/ready
curl -fsS http://127.0.0.1:15178/ready
curl -fsS -H 'Host: xianyu.enjoygoo.win' http://127.0.0.1/healthz
curl -fsS -H 'Host: xianyu.enjoygoo.win' http://127.0.0.1/readyz
```

## 发布流水线

固定流程：

```text
本地开发
  -> 本地测试
  -> 本地构建 Web/Crawler
  -> git commit
  -> git push
  -> C: git pull --ff-only
  -> 上传 dist
  -> 执行 docs/deploy.md 中的非代码操作
  -> systemd 重启
  -> 健康检查与浏览器冒烟测试
```

禁止：

- 在 C 上执行 Vite/TypeScript 构建。
- 在 C 上直接修改并提交代码。
- 只执行 `git pull` 而漏掉 SQL、systemd、Nginx、权限或目录操作。

## 回滚

代码回滚：

```sh
cd /var/www/xianyu
git switch --detach <上一个正常版本>
systemctl restart xianyu-api xianyu-worker xianyu-crawler
```

发布时保留：

- 上一个版本的 Git commit。
- 上一个版本的 Web/Crawler `dist` 发布包。
- Nginx 配置副本。
- `.env` 和 secrets 加密备份。
- 迁移前 MySQL 逻辑备份。

数据库迁移不一定可逆。若迁移包含不可逆 SQL，回滚代码前必须确认数据库兼容，不能只切换 Git 版本。

## 验收门槛

允许首次启动：

- 物理内存可用至少 1.2GB，或已配置 4GB以上的 Swap 且经过压力测试。
- `CRAWLER_MAX_CONCURRENCY=1`。
- API、Worker、Crawler、Nginx 健康检查全部通过。
- DNS 两套公共解析均返回服务器 C。

暂不允许启动 Crawler：

- 可用内存低于 1GB 且无 Swap。
- 另一个项目的 Chrome 或 PHP 队列仍在持续占用大内存。
- 无法完成单浏览器内存峰值测试。

正式稳定运行建议：

- 内存 8GB。
- Swap 2-4GB。
- Crawler 并发 1。
- 配置进程内存监控、OOM 告警和每日 MySQL 备份。

## 待确认

1. DNS 仍返回 `166.88.227.132`，需要等待新记录传播后复查。
2. 仓库强制约定引用的 `docs/important.md` 当前不存在，正式部署前应恢复或由项目负责人确认无遗漏决策。
3. MySQL 数据库、生产 secrets、systemd、Nginx、证书和 Playwright 均尚未在服务器 C 配置。
