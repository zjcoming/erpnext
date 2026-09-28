# 老板客户、供应商及采购单打印权限修复（2026-09-28）

2026-09-28 15:27:43（北京时间）完成生产发布，站点为 `https://erp.hengsuankeji.com`。

## 原因与改动

`Process Simplification Owner` 模板已有客户、供应商读取权限和采购订单操作权限，但遗漏客户、供应商创建/编辑及采购订单打印。旧老板账号杨耀另有原生管理角色，掩盖了该问题；新老板账号刘圣建没有这些历史角色，因此出现入口缺失。

- 为老板模板补充 Customer、Supplier 的 create/write，Purchase Order 的 print。
- 为客户快速录入补充 Customer Group、Territory 的 read/select，支持关联字段选择。
- 沿用安装/迁移时的模板同步逻辑，新老板账号继承同一规则。
- 生产仅同步这五种单据的老板规则，未执行全量迁移，未改动用户的角色分配、公司范围或其他岗位规则。

## 源码与镜像

- 分支：`rc/develop-v16`。
- 修复提交：`3af267be68be952c939170d51709b33278bffac8`。
- 基础镜像：`hengsuan/erpnext:v16.33.0-hs.20260927.2`。
- 最终镜像：`hengsuan/erpnext:v16.33.0-hs.20260928.1`。
- 镜像 ID：`sha256:1bd323182e68cc0c57a5e1cc8099b45a21b9b92adf1ebf97b6b8f192ef5f1856`。
- Frappe 16.32.0、ERPNext 16.33.0 保持不变；镜像内 371 个应用源码文件与修复提交的完整指纹一致：`0d73bde8ceff32d0d70fa64f3a390153e7c91b687b226ea298c78fd4e0a723c3`。

镜像仅覆盖权限模块及其测试文件；工作区同期的移动端页面改动未包含在此发布中。

## 验证

- 本地隔离 v16 站点运行完整 `test_management_access` 模块，27 项测试通过。首次运行暴露测试站缺少既有测试依赖的旧 `Production Supervisor` 角色，补齐测试环境 fixture 后全量通过；测试开关随后关闭。
- 新测试以仅有老板角色的账号通过原生 `frappe.client.insert` 创建客户/供应商，覆盖联系人、地址和后续编辑；不依赖 System Manager。
- 测试真实已提交采购单的“工厂采购订单”打印格式，并验证跨公司打印仍被拒绝。销售、仓库、生产岗位未获得本次老板权限，系统设置写入及客户/供应商删除未开放。
- 生产以刘圣建、杨耀各自权限上下文检查原生 create/write/print 与前端启动权限列表，全部通过。两账号均成功渲染现有采购单 `PUR-ORD-2026-00005` 的“工厂采购订单”打印内容；刘圣建仍没有 System Manager。
- 浏览器在杨耀现有会话中使用原生权限调试工具，指定刘圣建及该采购单，最终 `has_permission` 返回 True，确认 print=1；切换供应商创建检查，同样返回 True，create/write 均为 1。
- 六个应用服务全部运行最终镜像，容器就绪检查通过；本机及公网 HTTPS ping 均返回 `pong`。

生产验证未创建测试业务单据，未代替刘圣建进行交互登录，未向实体打印机发送任务。

## 数据保护与回退

维护期间停止业务服务，备份数据库、公开附件、私有附件及站点配置，并校验压缩包与 SHA-256。保存五种单据的原权限记录及旧镜像配置。修复脚本 apply/verify/rollback 已在隔离站点演练。

权限应用与独立复核均核对 27 张业务及账号表的逐行摘要，全部与发布前一致；五种单据的其他角色规则也保持一致。生产未触发回退，未执行备份恢复。服务恢复前关闭维护模式。

服务器证据目录：`/opt/hengsuan-erp/releases/20260928.1-owner/`。其中 `backup/`、`backup-sha256.txt`、`before.json`、`applied.json`、`verified.json`、`candidate-gate.txt`、`image-id.txt`、`deploy.log`、`completed.txt` 及旧配置保留在受限目录，不提交 Git。

上线后刷新页面即可重新加载权限；若旧页面仍没有入口，退出并重新登录。
