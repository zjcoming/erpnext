# ERPNext 本地开发手册

## 当前开发环境

- Windows 仅作为宿主机，使用 WSL2 发行版 `Ubuntu-24.04`。
- 实际运行和修改的源码位于 WSL，不要修改当前 Windows 副本来验证功能。
- Frappe Docker：`/home/frappe/projects/frappe_docker`
- Bench：`/home/frappe/projects/frappe_docker/development/frappe-bench`
- ERPNext：`/home/frappe/projects/frappe_docker/development/frappe-bench/apps/erpnext`
- Frappe：`/home/frappe/projects/frappe_docker/development/frappe-bench/apps/frappe`
- 站点：`development.localhost`
- 地址：`http://development.localhost:8000`
- 开发容器：`devcontainer-frappe-1`
- Compose 文件：`/home/frappe/projects/frappe_docker/.devcontainer/docker-compose.yml`

所有 `bench` 命令必须在 `devcontainer-frappe-1` 容器的
`/workspace/development/frappe-bench` 目录执行。

## 每次开机启动

在 PowerShell 中进入 WSL：

```powershell
wsl -d Ubuntu-24.04
```

在 WSL 中启动数据库、Redis 和开发容器：

```bash
cd /home/frappe/projects/frappe_docker
docker compose -f .devcontainer/docker-compose.yml up -d
docker compose -f .devcontainer/docker-compose.yml ps
```

然后以前台方式启动 Bench，保留这个终端查看实时日志：

```bash
docker exec -it devcontainer-frappe-1 bash
cd /workspace/development/frappe-bench
bench start
```

浏览器访问：

```text
http://development.localhost:8000
```

如果提示端口已占用，说明 Bench 很可能已经启动。检查：

```bash
docker exec devcontainer-frappe-1 ps aux | grep honcho
docker exec devcontainer-frappe-1 tail -n 100 /workspace/development/frappe-bench/logs/bench-start.log
```

## 后台启动 Bench

不想一直保留终端时，可以在 WSL 中执行：

```bash
docker exec -d devcontainer-frappe-1 bash -lc \
  'cd /workspace/development/frappe-bench && bench start >> logs/bench-start.log 2>&1'
```

启动前先检查是否已有 `honcho start` 进程，避免重复启动：

```bash
docker exec devcontainer-frappe-1 ps aux | grep '[h]oncho start'
```

查看后台日志：

```bash
docker exec -it devcontainer-frappe-1 bash -lc \
  'cd /workspace/development/frappe-bench && tail -f logs/bench-start.log'
```

## 打开源码开发

推荐从 WSL 打开 VS Code：

```bash
cd /home/frappe/projects/frappe_docker/development/frappe-bench
code .
```

主要目录：

```text
apps/erpnext    ERPNext 业务功能、单据、报表和制造模块
apps/frappe     Frappe 框架、通用表单、Grid、权限和前端基础组件
sites           站点配置和站点文件
logs            Bench、Web、Worker 和 Scheduler 日志
```

定制产品时优先创建独立自定义 App，不要长期直接修改 Frappe/ERPNext 核心，
否则后续升级和合并上游代码的成本会明显增加。

## 修改后的常用命令

先进入容器：

```bash
docker exec -it devcontainer-frappe-1 bash
cd /workspace/development/frappe-bench
```

修改 Python、DocType、Patch 或数据库结构后：

```bash
bench --site development.localhost migrate
bench --site development.localhost clear-cache
```

修改 ERPNext 前端 JS、SCSS、Vue 或构建资源后：

```bash
bench build --app erpnext
bench --site development.localhost clear-cache
```

修改 Frappe 通用前端后：

```bash
bench build --app frappe
bench --site development.localhost clear-cache
```

两个 App 都改动时：

```bash
bench build
bench --site development.localhost migrate
bench --site development.localhost clear-cache
```

开发模式下 `bench start` 包含资源监听，部分前端修改会自动构建；若浏览器未更新，
先按 `Ctrl+F5` 强制刷新，再手动执行对应的 `bench build`。

## 查看状态和日志

```bash
cd /home/frappe/projects/frappe_docker
docker compose -f .devcontainer/docker-compose.yml ps
docker compose -f .devcontainer/docker-compose.yml logs --tail=100
```

容器内：

```bash
cd /workspace/development/frappe-bench
bench version
tail -f logs/bench-start.log
tail -f logs/worker.error.log
tail -f logs/frappe.log
```

## Git 检查

ERPNext 和 Frappe 是两个独立 Git 仓库：

```bash
cd /home/frappe/projects/frappe_docker/development/frappe-bench
git -C apps/erpnext status
git -C apps/frappe status
```

当前 Frappe 中有一个尚未提交的本地修复，用于解决可编辑 Grid 中 `CNY`
与金额重叠的问题：

```text
apps/frappe/frappe/public/js/frappe/form/grid_row.js
apps/frappe/frappe/public/scss/common/grid.scss
```

更新 Frappe 或切换分支前必须先处理或保存这两个改动，不要直接覆盖。

## 停止环境

如果 Bench 在前台运行，先在该终端按 `Ctrl+C`，然后：

```bash
cd /home/frappe/projects/frappe_docker
docker compose -f .devcontainer/docker-compose.yml down
```

日常停止不要添加 `-v`。下面的命令会删除 MariaDB volume 和业务资料，除非明确要
重置全部数据，否则严禁执行：

```bash
docker compose -f .devcontainer/docker-compose.yml down -v
```

## 最短日常流程

```bash
wsl -d Ubuntu-24.04
cd /home/frappe/projects/frappe_docker
docker compose -f .devcontainer/docker-compose.yml up -d
docker exec -it devcontainer-frappe-1 bash
cd /workspace/development/frappe-bench
bench start
```

然后访问 `http://development.localhost:8000`，在另一个 WSL 终端中使用
`code /home/frappe/projects/frappe_docker/development/frappe-bench` 打开源码。
