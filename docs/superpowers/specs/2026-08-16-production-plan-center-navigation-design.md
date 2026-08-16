# 生产计划中心导航设计

## 背景

简化生产流程已将 Production Plan 作为计划日期、层级工单、可开工判断和缺料计算的权威入口。生产工作台中的创建动作实际会创建 Production Plan，并由计划生成成品及半成品层级工单，不再直接创建独立 Work Order。

当前导航名称“生产工作台”未明确表达这一前置关系；侧边栏虽配置了 `manufacturing` 图标，但当前 Frappe 侧边栏使用的 Lucide 图标集中没有该名称，因此界面没有显示图标。

## 决策

- 自定义页面显示名称统一改为“生产计划中心”。
- 侧边栏图标改为 Lucide 中有效的 `factory`。
- 路由和内部标识继续使用 `production-workbench`，避免破坏订单工作台跳转、收藏和既有链接。
- 内部创建动作继续使用 `create_work_order` 作为兼容 API 名称；用户可见文案仍为“创建生产计划”。

## 修改范围

以下用户可见入口统一改名：

1. Workspace Sidebar 中的页面标签与图标。
2. 流程简化 Workspace 核心流程卡片中的页面标签。
3. Page 元数据、页面 Python 上下文和浏览器页面标题。
4. 既有站点导航修复补丁，使数据库中已经安装的记录同步更新。

不修改以下内容：

- 页面路由 `production-workbench`。
- ERPNext 标准 Production Plan 和 Work Order 单据名称。
- Production Plan 驱动的可开工、缺料与底层采购计算逻辑。
- 旧独立工单的迁移策略；它们仍显示为“旧工单未纳入计划”。

## 兼容与数据更新

导航补丁必须可重复执行：无论导航项此前缺失、仍叫“生产工作台”，或图标仍为 `manufacturing`，执行后都应得到“生产计划中心”与 `factory`。补丁不新建或迁移业务单据。

## 验证

- 集成测试验证补丁会更新侧边栏标签、`factory` 图标、Workspace 卡片标签和 Page 标题，同时保持页面位于“缺料采购”之前。
- 前端测试验证页面自身标题为“生产计划中心”，路由仍为 `production-workbench`。
- 运行 `process_simplification` 全量单元测试、集成测试、前端测试与资源构建。

