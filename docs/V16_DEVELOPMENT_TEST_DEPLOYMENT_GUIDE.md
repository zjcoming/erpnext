# ERPNext v16 二次开发、测试与部署基线手册

本文档是本项目后续开发、测试、发布和正式部署的操作基线。除非经过专项升级评审并完成全量回归，不得改变本文记录的 Frappe、ERPNext、运行镜像和数据库大版本。

## 1. 适用范围与结论

- 长期集成分支：`rc/develop-v16`
- 开发站点：`development.localhost`
- 开发访问地址：`http://development.localhost:8000`
- 官方 ERPNext/Frappe 源码原则上不修改；二次开发集中在 `custom_apps/process_simplification`。
- 当前 `docker-compose.yml` 是开发环境，不是生产部署编排，禁止直接用于公网生产环境。
- 生产发布必须使用不可变 Git 标签、完整提交 SHA 和镜像 digest，不允许直接部署一个持续变化的分支。

## 2. 架构和代码边界

依赖关系：

```text
process_simplification（业务简化、报工、工资、计划、缺料）
        ↓
ERPNext（销售、采购、库存、BOM、工单、Job Card、交货等业务事实）
        ↓
Frappe Framework（DocType、ORM、权限、页面、API、队列、迁移）
        ↓
Python / Node.js / MariaDB / Redis
```

仓库与运行目录：

```text
/home/frappe/projects/erpnext
├── erpnext/                         ERPNext 官方业务源码
├── banking/                         ERPNext v16 银行前端模块
├── custom_apps/
│   └── process_simplification/      本项目二次开发代码
├── deployment/
│   └── v16-baseline.env             冻结版本清单
├── docs/                            项目维护文档
├── docker-compose.yml               开发环境编排
└── development/                     自动生成的 Bench、站点和日志，不提交 Git
```

运行时 Bench 由 Compose 自动创建：

```text
development/frappe-bench/
├── apps/frappe/                     固定提交的 Frappe
├── apps/erpnext -> 仓库根目录
├── apps/process_simplification -> custom_apps/process_simplification
├── sites/
└── logs/
```

### 2.1 允许修改

- `custom_apps/process_simplification/`
- `docker-compose.yml`、部署脚本和项目文档
- 为 Custom App 服务的测试、Patch、Fixture、Workspace 和 Hook

### 2.2 默认禁止修改

- `erpnext/` 下的官方 ERPNext 业务代码
- `development/frappe-bench/apps/frappe/` 下的 Frappe 运行时源码
- MariaDB 中由 ERPNext 管理的状态、库存、完成数量和财务字段

只有 Hook、Override、DocType 扩展等机制确实无法实现时，才评审核心修改；必须单独记录原因、影响范围和未来维护方法。

## 3. 冻结版本

`deployment/v16-baseline.env` 是环境版本清单的唯一事实来源。`docker-compose.yml` 中的值必须与它保持一致。

| 组件 | 当前固定值 | 固定方式 |
| --- | --- | --- |
| ERPNext | `16.33.0` | 官方基线提交 `b24c9eba551905e256e336ff170a91a92d197a2f` |
| Frappe | `16.32.0` | 提交 `5cba016e86b54b57f34a3864282b92300ef20fb0` |
| process_simplification | 当前开发版本 `0.0.1` | 整个仓库的 Git 提交/发布标签 |
| 首个正式 Custom App 版本 | 建议 `1.0.0` | 正式验收通过后修改版本并打发布标签 |
| Bench | `5.29.1` | 固定 Bench 镜像 digest |
| Python | `3.14.2` | 固定 Bench 镜像 digest |
| Node.js | `24.13.0` | 固定 Bench 镜像 digest |
| MariaDB | `11.8.8` | 固定镜像 digest |
| Redis | `8.8.0` | 固定镜像 digest |

当前镜像：

```text
Bench:
docker.io/frappe/bench@sha256:85bd55c881bc99948c59ac3efc12cfb80e88b785952876b932a96d74b27c8538

MariaDB:
docker.io/mariadb@sha256:be1ef4fe5f14589325c08a41c76334097ce66c86264b75e1d28342c742782a61

Redis:
docker.io/redis@sha256:09160599abd229764c0fb44cb6be640294e1d360a54b19985ab4843dcf2d90f1
```

### 3.1 为什么版本号和 Git 提交都要固定

- `16.32.0` 这样的版本号说明功能版本，但标签理论上可能被错误重建。
- 完整 Git SHA 精确指定源码内容。
- Docker digest 精确指定镜像二进制内容，避免同名 tag 漂移。
- Custom App 在本仓库中开发，因此最终以“发布标签指向的完整仓库提交”为准，不能只看 `__version__`。

### 3.2 pyproject 中的依赖范围不是生产锁文件

`custom_apps/process_simplification/pyproject.toml` 当前声明：

```toml
[tool.bench.frappe-dependencies]
frappe = ">=16.32.0,<17.0.0"
erpnext = ">=16.33.0,<17.0.0"
```

这表示 Custom App 的兼容边界，不表示运行环境可以自动升级。生产实际版本仍由以下内容精确锁定：

1. `deployment/v16-baseline.env`
2. `docker-compose.yml` 中的 commit/version 检查
3. 发布 Git 标签对应的完整仓库 SHA
4. Docker 镜像 digest

## 4. Custom App 版本策略

版本使用语义化版本 `MAJOR.MINOR.PATCH`：

- `PATCH`：兼容性缺陷修复，例如 `1.0.0 -> 1.0.1`
- `MINOR`：向后兼容的新功能，例如 `1.0.1 -> 1.1.0`
- `MAJOR`：不兼容的数据结构、接口或业务规则变更，例如 `1.x -> 2.0.0`

当前 `process_simplification` 是 `0.0.1`，代表仍处于开发基线。第一次正式上线前建议执行：

1. 完成预生产数据演练和业务验收。
2. 将 `custom_apps/process_simplification/process_simplification/__init__.py` 改为 `1.0.0`。
3. 执行本文所有自动化和人工验收。
4. 合入 `rc/develop-v16`。
5. 创建唯一发布标签，例如：

```bash
git tag -a custom-v16-ps-1.0.0 -m "ERPNext v16 + process_simplification 1.0.0"
git push origin custom-v16-ps-1.0.0
```

生产环境必须检出标签或标签对应的完整 SHA，不能直接跟随 `rc/develop-v16` 分支移动。

## 5. 分支策略

### 5.1 分支职责

| 分支/标签 | 用途 |
| --- | --- |
| `rc/develop-v16` | v16 长期集成和验收基线 |
| `codex/<feature>` 或 `feature/<feature>` | 单个开发需求 |
| `fix/<issue>` | 缺陷修复 |
| `custom-v16-ps-X.Y.Z` | 不可变正式发布标签 |

### 5.2 开始一个需求

```bash
cd /home/frappe/projects/erpnext
git switch rc/develop-v16
git pull --ff-only origin rc/develop-v16
git switch -c feature/<功能名称>
```

不要在旧的 `rc/develop` 或 ERPNext/Frappe 官方不稳定分支上继续开发。

### 5.3 完成一个需求

```bash
git diff --check
git status --short
```

完成测试后提交功能分支，通过评审再合回 `rc/develop-v16`。合并后必须再次执行迁移、构建和关键回归。

## 6. 开发环境操作

### 6.1 启动

```bash
cd /home/frappe/projects/erpnext
docker compose up -d
docker compose ps -a
```

运行服务：

- `frappe`：Web、Socket.IO、Scheduler、Worker
- `mariadb`：数据库
- `redis-cache`：缓存
- `redis-queue`：队列和实时消息
- `init-permissions`：一次性目录权限初始化；`Exited (0)` 是正常状态

首次启动会安装依赖、建站、安装 App、执行迁移和构建，耗时明显长于日常启动。

### 6.2 查看日志

```bash
docker compose logs --tail=200 frappe
docker compose logs -f frappe
```

### 6.3 停止和删除的区别

```bash
# 停止容器，保留容器和数据
docker compose stop

# 删除容器和网络，保留 MariaDB 数据卷
docker compose down

# 删除容器、网络和 MariaDB 数据卷；数据不可恢复
docker compose down -v
```

只有明确允许丢弃本地数据时才能执行 `docker compose down -v`。

### 6.4 进入 Bench 执行命令

```bash
docker compose exec -T frappe bash -lc \
'cd /workspace/erpnext/development/frappe-bench && <bench 命令>'
```

不要直接在宿主机的 `development/frappe-bench` 中安装依赖；依赖安装应在固定镜像内完成。

## 7. 不同改动需要做什么

| 改动 | 必做操作 |
| --- | --- |
| 纯 Python 业务逻辑 | 相关单元/集成测试，确认开发服务器重载成功 |
| 前端 JS/CSS | `bench build --app process_simplification`，浏览器强制刷新 |
| DocType JSON | `bench --site development.localhost migrate` |
| Hook、Fixture、Workspace、Sidebar | migrate、clear-cache、build、浏览器导航复测 |
| Patch | 在空库和现有数据副本上分别执行 migrate |
| Python/Node 依赖 | 更新锁定文件，在固定镜像内重新安装并全量测试 |
| 权限和角色 | 至少使用 Administrator、Production Worker、Production Supervisor 分别验证 |
| 库存/生产数量 | 必须执行真实单据集成测试，不能只 Mock |

常用命令：

```bash
# 迁移
docker compose exec -T frappe bash -lc \
'cd /workspace/erpnext/development/frappe-bench && bench --site development.localhost migrate'

# 清缓存
docker compose exec -T frappe bash -lc \
'cd /workspace/erpnext/development/frappe-bench && bench --site development.localhost clear-cache && bench --site development.localhost clear-website-cache'

# 仅构建 Custom App
docker compose exec -T frappe bash -lc \
'cd /workspace/erpnext/development/frappe-bench && bench build --app process_simplification'
```

## 8. 测试基线

### 8.1 JavaScript 测试

```bash
cd /home/frappe/projects/erpnext/custom_apps/process_simplification
node --test process_simplification/tests/js/*.test.js
```

当前基线：`64/64` 通过。

### 8.2 Python 全量测试

```bash
docker compose exec -T frappe bash -lc \
'cd /workspace/erpnext/development/frappe-bench && bench --site development.localhost run-tests --app process_simplification --test-category all'
```

当前基线：

- Python 单元测试：`143/143`
- Python 集成测试：`50/50`
- Python 合计：`193/193`
- 自动化总计：`257/257`

测试数量以后可以增加，但不得无说明减少。若减少，必须在评审中说明删除或合并了哪些测试以及原因。

### 8.3 浏览器验收

至少覆盖：

1. 流程简化工作区
2. 快速开单
3. 订单工作台
4. 生产计划中心
5. 缺料采购
6. Production Worker 的“我的报工”
7. Production Supervisor 的“报工审核”
8. 工序计价规则
9. 月度工资汇总

浏览器必须使用 `http://development.localhost:8000`，不要用 `127.0.0.1` 代替站点主机名，否则可能出现 Socket.IO namespace 错误。

## 9. 业务开发规则

### 9.1 标准单据是业务事实

继续使用 ERPNext 标准单据：

- 销售：Sales Order、Delivery Note
- 采购：Material Request、Purchase Order、Purchase Receipt
- 生产：BOM、Production Plan、Work Order、Job Card
- 库存：Stock Entry、库存预留

Custom App 负责简化入口、业务编排和补充数据，不复制另一套库存、生产或销售主账。

### 9.2 遵守单据生命周期

ERPNext 单据通常遵循：

```text
Draft -> Submitted -> Cancelled
docstatus: 0 -> 1 -> 2
```

禁止用 SQL 直接改库存、工单完成量、Job Card 状态或财务字段。使用 Frappe Document API 和 ERPNext 标准 Mapper/Service。

### 9.3 权限边界

- 普通用户查询优先使用 `frappe.get_list`，让 Frappe 权限过滤生效。
- 谨慎使用 `frappe.get_all` 和直接 SQL。
- 生产工人和主管角色保持隔离，不授予宽泛的 System Manager/Manufacturing 权限作为绕过方案。

### 9.4 工资边界

- `Job Card.hour_rate` 是制造成本，不是工人工资。
- 工资以主管批准的报工、批准数量/工时和工序计价规则为准。
- 多人共同完成同一 Job Card 时，按各自批准份额计价，不得重复计算完整产量。

## 10. Developer Mode 和元数据

Compose 会把站点 `developer_mode` 固定为 `0`，避免安装或迁移时自动改写 Workspace/DocType JSON，污染 Git 工作区。

如确实需要通过 UI 创建或修改 DocType：

1. 在独立开发分支和可丢弃站点上临时开启 Developer Mode。
2. 完成 UI 修改后导出 DocType、Fixture 或 Workspace 到 Custom App。
3. 检查 Git diff，删除纯时间戳等无意义变化。
4. 关闭 Developer Mode。
5. 在全新站点重新安装验证，确认元数据可以由代码重建。

数据库里的 UI 修改如果没有导出到 Git，不能视为已经交付。

## 11. 正式部署前置条件

当前 `docker-compose.yml` 使用开发服务器和开发口令，不满足生产要求。第一次正式部署前必须新增并验收独立的生产编排，例如 `docker-compose.prod.yml`，至少包含：

- HTTPS 反向代理（Nginx/Traefik）
- Gunicorn/Web Backend
- 独立 Socket.IO 服务
- Scheduler
- Default/Short/Long Queue Worker
- MariaDB 持久化卷
- Redis Cache 和 Redis Queue
- Sites、Private Files、Public Files、Logs、Backups 持久化
- 健康检查、自动重启和日志轮转
- 外部化密钥和密码，不写入 Git

正式部署前还必须确认：

- 域名、TLS 证书和防火墙
- SMTP、对象存储等外部服务
- 数据库备份与文件备份
- 备份恢复演练
- 监控、告警、磁盘容量和日志保留
- RPO/RTO 和维护窗口

## 12. 正式发布流程

### 12.1 发布前

1. 从 `rc/develop-v16` 创建候选版本。
2. 将 Custom App 版本更新到目标版本。
3. 在预生产环境使用生产数据脱敏副本演练迁移。
4. 执行 257 项以上自动化测试和完整业务验收。
5. 检查 ERPNext/Frappe 官方源码没有非预期修改。
6. 创建带注释 Git 标签并推送。
7. 记录标签、完整 SHA、镜像 digest、数据库版本和迁移清单。

### 12.2 部署顺序

1. 备份 MariaDB、Public Files、Private Files 和站点配置。
2. 验证备份文件完整性，并确认能够恢复。
3. 开启维护模式，停止 Scheduler 和 Worker 接收新任务。
4. 生产主机检出精确发布标签/SHA。
5. 拉取或构建固定 digest 的生产镜像。
6. 先运行数据库迁移，再启动 Web、Socket.IO、Scheduler 和 Worker。
7. 清缓存并执行资产构建或加载已构建资产。
8. 验证登录、权限、后台任务、Socket.IO 和核心业务链路。
9. 验收通过后关闭维护模式。

典型 Bench 操作顺序：

```bash
bench --site <正式站点> set-maintenance-mode on
bench --site <正式站点> backup --with-files --compress
bench --site <正式站点> migrate
bench --site <正式站点> clear-cache
bench --site <正式站点> clear-website-cache
bench --site <正式站点> set-maintenance-mode off
```

这些命令必须在未来的生产容器/Bench 路径中执行，不能原样假设当前开发 Compose 就是生产环境。

## 13. 正式部署验收

上线后至少验证：

- `/login` 返回 200，TLS 和域名正确
- Frappe/ERPNext/Custom App 版本与发布清单一致
- 数据库迁移没有未执行 Patch
- Administrator、Production Worker、Production Supervisor 权限正确
- 快速开单到 Sales Order
- 缺料计算到 Material Request/Purchase Order/Purchase Receipt
- Production Plan、Work Order、Job Card 和报工审核
- 工序计价和月度工资汇总
- 库存预留与 Delivery Note
- Worker、Scheduler、Redis Queue 和 Socket.IO 正常
- 错误日志没有新增持续异常

## 14. 回滚原则

代码回滚不等于数据库回滚。若新版本已经执行不可逆数据库迁移，仅切回旧 Git 提交可能导致旧代码读取新结构而失败。

推荐策略：

1. 优先编写向前兼容、可重复执行的 Patch。
2. 删除字段、改名、批量转换等破坏性变更必须提供专门回滚方案。
3. 发布前保留匹配该版本的数据库和文件全量备份。
4. 需要整体回滚时，同时恢复代码标签、数据库、Public Files 和 Private Files。
5. 回滚后重新执行版本、登录、队列和核心业务冒烟测试。

## 15. 禁止操作

在冻结基线上不要执行：

```bash
bench update
bench switch-to-branch
git pull upstream
docker pull <使用 latest 或可漂移 tag 的镜像>
```

同时禁止：

- 单独升级 Frappe 或 ERPNext
- 使用 `latest` 镜像
- 直接修改运行目录下的 Frappe 源码
- 把 `development/`、数据库文件、站点密钥或备份提交到 Git
- 未备份就执行 `docker compose down -v`
- 只依赖“页面能打开”判断业务逻辑通过
- 未执行 migrate 就认为 DocType/Workspace 变更已部署

## 16. 每次发布的记录模板

```text
Custom App 版本：
Git 标签：
完整仓库 SHA：
ERPNext 版本/官方基线 SHA：
Frappe 版本/SHA：
Bench/MariaDB/Redis 镜像 digest：
数据库备份位置与校验值：
Public/Private Files 备份位置与校验值：
执行的 Patch：
自动化测试结果：
浏览器验收结果：
部署时间：
部署人：
回滚点：
遗留问题：
```

## 17. 当前待办

- [ ] 第一次正式上线前，将 `process_simplification` 从 `0.0.1` 升为 `1.0.0`
- [ ] 新增独立的生产 Docker/部署编排
- [ ] 建立预生产环境和生产数据脱敏迁移演练
- [ ] 建立自动备份、异地备份和恢复演练
- [ ] 建立生产监控、告警和日志保留策略
- [ ] 正式上线前创建不可变 Git 发布标签
