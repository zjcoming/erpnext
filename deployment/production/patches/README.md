# Frappe 并发事务补丁

适用基线：Frappe `v16.32.0`，提交 `5cba016e86b54b57f34a3864282b92300ef20fb0`；已验证数据库为 MariaDB `11.8.8`、`REPEATABLE-READ`、`innodb_snapshot_isolation=ON`。升级 Frappe 或 MariaDB 时重新核对补丁及测试，不能直接放宽校验。

`frappe-patches.json` 固定八个原始文件和补丁后文件的 SHA-256。`apply_frappe_patches.py` 在任何写入前检查全部文件，拒绝未知修改和部分应用状态；完整应用后的再次执行只验证，不重复修改。生产 Dockerfile 在固定上游提交后应用补丁，运行中的站点文件不会被本工具自动替换。旧六文件补丁镜像须从固定上游重新构建，不能将旧镜像当作未修改源码再次应用完整补丁。

## 修复范围

- **编号竞争**：MariaDB 的 `tabSeries` 使用原连接、原事务内的原子递增与条件递减。只有这两条计数器语句采用 `SET STATEMENT innodb_snapshot_isolation=OFF FOR ...`；MariaDB 在语句结束或报错时恢复设置。业务表继续使用原快照，编号变化和单据一起回滚。新增系列并发创建、最后一个编号删除回退以及异常 `NULL` 计数器的原报错行为都有覆盖。没有独立提交编号，没有请求重放。Frappe 的写入计数、表缓存失效仍按实际 INSERT/UPDATE 分类执行。
- **同账号登录竞争**：在 `HTTPRequest`、认证和请求钩子之前，只在尚未建立连接、没有写入、事务回调或禁用事务控制时执行。先用只读事务查账号候选名并完整回滚这一次查询，再按名字顺序通过主键锁住 `User`，随后锁 `__Auth` 密码记录。这样避开 OR 条件锁表扫描，其他员工可以并行登录。重新核对候选映射后，继续原来的账号选择、权限、密码、禁用状态、IP/时段、二次验证和钩子流程。别名在中途变化会在认证前返回通用认证错误。
- **锁顺序**：User 密码写入、删除和重命名统一使用 `User → __Auth`；会话刷新采用 `User → Sessions`，避免登录与密码重置、退出其他会话产生反向等待。所有这些锁均使用普通快照规则，遇到业务冲突仍然报错并回滚。
- **会话一致性**：新 SID 在数据库成功提交后才写入 Redis。MariaDB 的 1020/1213 会回滚整个事务，不能通过保存点恢复；登录元数据更新保留原异常，由事务拥有方处理，避免被后续 1305 覆盖或者留下只有缓存、没有数据库记录的 SID。其他数据库后端继续使用原保存点行为。
- **销售订单新草稿快照恢复**：REST v1 `create_doc` 为 Sales Order 调用可选 App hook；Desk 将原 `savedocs` 的保存主体提取为 `_savedocs`，保持临时名称、附件映射、权限、onload 和返回格式，遥测在恢复循环外执行一次。实际恢复策略在 `process_simplification.sales_order_creation`，只对这两个真实 HTTP 新草稿入口启用，首次遇到明确的 Sales Order 父表 INSERT 1020 后完整回滚，再从原始输入构造新 Document，最多两次尝试。它不自行提交；成功仍由原请求边界提交。

补丁未修改编号格式、业务校验、库存与财务计算、登录权限策略、全局数据库参数或 Web 进程数。它不承诺任意后台任务、单点登录或自定义 `before_login` 改写身份的路径具有相同并发效果；新增这类钩子时应复验锁定候选与最终认证身份一致。元数据刷新和其他业务写入遇到真实冲突仍会失败，不会静默成功或自动重放。

### 新草稿恢复边界

- 入口必须没有已有数据库写入、事务回调、待发布页面变更或嵌套恢复；只允许 MariaDB 的真实 REST v1 POST 或 Desk 新单 Save。内部 `Document.insert()`、REST v2、更新、删除、提交以及有自己幂等和提交逻辑的快速开单均不套用恢复。
- 重试策略依赖当前固定的 Frappe/ERPNext/Custom App 代码。额外应用、覆盖控制器、未知权限/请求/认证钩子、未知父表或子表前置事件、自定义子表、启用的 Server Script/Webhook 或前置 Method Notification 会保守保留原行为。允许的原生前置钩子使用完整函数路径白名单，支持 Frappe 的 tuple DocType 作用域。它不是任意第三方 Python 网络副作用的检测器。
- 只在实际 `BaseDocument.db_insert` 与 `Database.sql` 堆栈同时对应 Sales Order 草稿父表 INSERT、底层错误码为 1020 时考虑恢复。1205、1213、1062、校验/权限错误和子表错误均不恢复。SQL 观察仅绑定当前请求的数据库实例；COMMIT、ROLLBACK、SET、DDL 等事务边界改变后停止恢复。正常的语句级 Series 计数仍保留。一旦 Sales Order 父表 INSERT 已成功，任何后续错误均不恢复，包括后置钩子创建第二张订单产生的 1020。
- 完整回滚清理数据库回调和值缓存；补充清理请求/文档/默认值/权限缓存、页面待发布集合，并恢复本次入口前的消息、返回值和 flags。保留认证会话、SID 和 CSRF 上下文。第二次重新检查安全配置和账号启用状态，新 Document 重新执行权限及业务校验。持续冲突的第二个原始异常正常抛出，没有第三次尝试，也没有假成功。
- 框架补丁与 App 代码需在同一发布镜像中交付。未打补丁的开发框架仍可使用 App，但 REST 入口不会调用恢复 hook，Desk 则保留原始单次保存。

## 验证方式

在项目根目录运行补丁安全测试（只修改临时源代码副本）：

```sh
python3 deployment/production/patches/test_patch_application.py
```

若 Frappe 源码不在默认的 `development/frappe-bench/apps/frappe`，通过 `PS_FRAPPE_SOURCE` 指定未打补丁的对应源码目录。可先验证目标：

```sh
python3 deployment/production/patches/apply_frappe_patches.py /path/to/frappe --check
```

`test_mariadb_contention.py` 必须在隔离环境中运行：使用打过补丁的 Frappe Python 环境，设置 `PS_TXN_DISPOSABLE=1` 和 `PS_TXN_DB_PASSWORD`，可通过 `PS_TXN_DB_HOST`、`PS_TXN_SITE`、`PS_TXN_SITES` 指向隔离数据库及站点。测试只创建并删除固定的 `ps_txn_patch_probe` 临时数据库；若同名库已经存在则直接停止，不清空它。数据库版本和隔离设置不符同样停止。该测试不操作站点业务表，不修改站点 `allow_tests`。

2026-09-10 验证结果：

- **15 项真实 MariaDB 事务测试通过**：并发新系列不重号、计数器回滚、删除最后编号、业务快照保留、业务写冲突仍然触发 1020、错误后隔离设置恢复、别名与禁用状态变更、锁超时、回调与已有写入保护、不同账号不互相阻塞、密码重置和会话刷新锁顺序。
- **3 项补丁安装安全测试通过**：只读验证、重复应用、未知源码与部分应用状态拒绝。
- **2 项反向验证成立**：将同一锁顺序测试替换为原始密码写入函数、原始会话刷新函数，两项均在预期的 `FOR UPDATE NOWAIT` 位置报 1205；补丁版本通过。这两个预期失败单独记录，不计入通过测试数量。
- 根任务另行验证了真实 HTTP：50 次并发登录全部成功，并逐一核对认证接口、数据库会话、Redis 会话与退出；草稿订单 50 次创建、50 次修改、50 次删除全部成功；9 项认证边界检查通过。

运行证据保存在本次输出目录 `outputs/performance-fix-20260910/`，包括 `transaction-probe.log`、`transaction-negative-control.log`、`patched-login-http.json`、`patched-draft-http.json`。这些是隔离环境验证结果，不代表生产服务器已经更新或完成生产全流程验收。

新草稿恢复补充验证（2026-09-10，`outputs/optimization-fix-20260910/`）：

- **23 项单元测试通过**：恢复/耗尽/原错、独立 Document 和原始输入、状态清理、账号停用、入口已有写入/回调、事务边界改变、首个父表写入成功后的嵌套冲突、Desk 遥测一次、临时子项名称、原生钩子与未知副作用配置、tuple/子表钩子和错误堆栈匹配。
- **8 次确定性真实 WSGI 请求恢复通过**：Administrator 与现有销售账号 × REST/Desk × 2 次；先建立旧快照，再由另一事务删除最后编号并提交。第一次实际父表 INSERT 1020，第二次 HTTP 200；每个标记仅有一张草稿、`after_insert` 一次，库存台账/总账/预留计数保持不变。修复前同机制 REST **4/4 失败**。这不是通过伪造异常验证。
- **3 项八文件补丁安装安全测试通过**：只读检查、完整应用/重复应用、未知及部分修改拒绝。

运行单元测试时使用 Frappe Python 环境，并以 bench 的 `sites` 为 cwd；不需要开启站点 `allow_tests`，测试用 Mock，不写业务单据：

```sh
../env/bin/python -m unittest process_simplification.tests.test_sales_order_creation -v
```

隔离 WSGI、真实 TCP 并发及生产发布是不同证据；生产是否生效以对应发布记录和镜像校验为准。

## ERPNext 批次补货补丁（2026-09-15）

`erpnext-patches.json` 固定 ERPNext v16.33.0 的 `erpnext/stock/reorder_item.py` 原始和修改后 SHA-256。`apply_erpnext_patches.py` 采用相同的拒绝未知源码、只读检查和重复应用校验。镜像同时保存此清单及其校验值。补丁只将不可用实物批次从补货预期库存中剔除，不改写库存余额、不重复扣除预留，也不改变普通物料规则。升级 ERPNext 时必须重新审查和回归。
