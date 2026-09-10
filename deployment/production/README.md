# 恒算 ERP 生产部署

本目录把应用代码固化进不可变镜像，并用单机 Docker Compose 运行 ERPNext 所需的 Web、WebSocket、后台队列、调度器、MariaDB 和 Redis。生产服务器只保存 Compose 配置、密钥和持久化卷，不在容器启动时拉取或编译源码。

## 当前生产并发基线：3×4

当前腾讯云 **4 核 / 8 GB** 单机部署固定使用 **3 个 Gunicorn Web 工作进程、每进程 4 线程**（`GUNICORN_WORKERS=3`、`GUNICORN_THREADS=4`）。2026-09-10 同镜像对照中，4×4 在 50、100 并发下响应更慢，已恢复 3×4；用户确认后续常规发布沿用此配置。环境示例、Compose、镜像启动脚本和镜像验收均采用此基线。

发布新镜像时保留服务器 `.env` 中的这两个值，不根据空闲内存自动增加 Web 进程。适用范围、压测结果及检查命令见 [Web 并发配置约定](web-concurrency.md)。

## 构建与验收

镜像默认从当前 Git 提交的归档构建；未提交文件、站点数据和本机密钥不会进入构建上下文。

```bash
./deployment/production/build-image.sh
./deployment/production/accept-image.sh
./deployment/production/export-image.sh
```

默认发布标签是 `hengsuan/erpnext:v16.33.0-hs.20260909.1`。正式构建应保留脚本输出的镜像 ID、源码提交和压缩包 SHA-256。

构建会对固定版本 Frappe 应用 `patches/` 中的事务并发补丁，并核对修改前后的文件 SHA-256；上游文件变化时会停止构建。镜像保留 `frappe-patches.json`，`release.env` 记录其校验值，验收脚本同时检查实际源码。升级 Frappe 时需重新审查补丁，不能跳过校验。2026-09-10 的工作台与事务修复验证见 [专项报告](../../custom_apps/process_simplification/docs/performance_fix_validation_20260910.md)。包含这些修复的发布必须使用新镜像标签，并先将拟发布代码纳入对应 Git 提交；当前构建脚本不会打包未提交改动。

## 服务器目录

```text
/opt/hengsuan-erp/
├── compose.yaml
├── .env
├── secrets/
│   ├── admin_password.txt
│   └── db_root_password.txt
└── releases/
```

从 `.env.example` 生成 `.env`，两个密码文件各写一行随机强密码并设为 `chmod 600`。`frontend` 默认只监听 `127.0.0.1:8080`，由 1Panel/OpenResty 为 `erp.hengsuankeji.com` 提供 HTTPS 反向代理。

加载镜像并启动：

```bash
docker load -i releases/hengsuan_erpnext_v16.33.0-hs.20260909.1.tar.gz
docker compose config --quiet
docker compose up -d --wait
docker compose ps -a
```

首次启动会创建站点、安装 ERPNext 和 `process_simplification`、执行迁移并启用调度器。后续使用相同命令会跳过建站并再次执行幂等迁移。

## 日常检查

```bash
docker compose ps -a
curl -fsS -H 'Host: erp.hengsuankeji.com' http://127.0.0.1:8080/api/method/ping
docker compose logs --since=30m backend queue-short queue-long scheduler
docker stats --no-stream
```

Compose 使用 Docker `local` 日志驱动并限制单容器日志大小，避免 120 GB 系统盘被日志占满。站点文件、日志、数据库和 Redis 队列分别保存在命名卷中。

## 备份

应用镜像不包含业务数据。日常备份应在运行中的 backend 容器执行：

```bash
docker compose exec -T backend bench --site erp.hengsuankeji.com backup --with-files --compress
```

备份产物包括数据库 SQL、公开文件、私有文件和配置。上传 COS 后才算完成异地备份；至少每月在隔离环境进行一次恢复演练。数据库卷快照可以作为附加保护，不能替代 ERPNext 逻辑备份。

## 发布更新

新版本使用新标签构建并验收。服务器先导入新镜像，修改 `.env` 中的 `ERP_IMAGE`，保留 `GUNICORN_WORKERS=3` 和 `GUNICORN_THREADS=4`，再运行：

```bash
docker compose up -d --wait
```

Compose 会先执行初始化检查和迁移，业务进程只在迁移成功后启动。回滚前必须确认新版本迁移是否允许旧代码继续使用，并保留升级前的完整逻辑备份。
