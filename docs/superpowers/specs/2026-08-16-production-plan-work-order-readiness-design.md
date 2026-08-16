# Production Plan 工单就绪与采购缺口设计

## 背景与问题

工厂的半成品全部作为独立库存物料，由独立 Work Order 制造并逐级入库。当前简化流程已经通过 ERPNext Production Plan 为成品和每层自产半成品创建独立工单，但生产工作台仍从销售订单成品 BOM 出发，使用完全展开后的底层物料判断“可开工”。

这会把两个不同问题混为一谈：

- 整条生产链所需的底层原料是否齐全。
- 某张具体工单的直接物料现在能否从来源仓发出。

已复现的订单中，工作台因为九种底层物料齐全而把顶层工单标记为“可开工”，但顶层工单直接需要的半成品“焊线线圈”库存为零，标准生产发料因此正确失败。

## 目标

1. 订单工作台以销售订单行为需求入口，展示其关联的活动 Production Plan 和生产进度。
2. 生产工作台按 Production Plan 展示完整 Work Order 依赖树。
3. “当前可开工”严格按每张 Work Order 的直接物料和当前实际库存判断。
4. “一键采购”覆盖完成所选或全部活动 Production Plan 所需的外购物料缺口。
5. 自产半成品不足只能形成上游生产依赖或资料阻塞，不能进入采购申请。
6. 复用现有共享库存按订单交期分配、在途供应识别和 Material Request 创建能力。

## 方案比较

### 方案 A：只修正工作台文案

继续使用完全展开 BOM，只把“可开工”改成“底层原料齐套”。改动最小，但仍不能告诉车间应该先做哪张工单，一键采购也继续绕过真实生产任务。不能满足工厂执行要求。

### 方案 B：Production Plan 作为组织单位，Work Order 作为执行真相

订单行关联 Production Plan，Production Plan 下展示工单树；逐工单读取直接必需物料判断当前就绪度，跨计划汇总尚未覆盖的外购物料用于采购。Production Plan 负责计划范围，Work Order 负责执行状态，库存台账负责最终数量事实。

这是推荐方案。它与 ERPNext 标准单据边界一致，也能同时回答“现在先做什么”和“最终还要买什么”。

### 方案 C：Production Plan 作为唯一状态真相

所有状态直接从 Production Plan 明细推导。查询较集中，但 Production Plan 是创建时的计划快照，不能可靠反映后续工单转移、完工、取消、补建和实际库存变化，因此不采用。

## 业务事实与关联边界

- Sales Order Item 是客户需求来源。
- Production Plan 是一批相关成品及半成品工单的计划容器。
- Work Order 是工序执行、直接用料和完工数量的事实来源。
- Bin、Stock Ledger Entry 和已提交 Stock Entry 是库存事实来源。
- Material Request、Purchase Order 和 Purchase Receipt 是未来外购供应事实来源。

不新增会改变 ERPNext 数量状态的强关联字段。现有原生关系已经足够：

- `Production Plan Item.sales_order` / `sales_order_item` 关联订单行。
- `Work Order.production_plan` 关联生产计划。
- `Work Order.sales_order` / `sales_order_item` 保留订单追溯。

一个订单行可能因分批、补产或重排关联多个 Production Plan。工作台必须聚合所有非取消计划和非终止工单，不能假设永久一对一。

## 工单依赖树

每份 Production Plan 构建一棵或多棵工单树：

- 节点：活动 Work Order。
- 父子关系：父工单直接物料中的自产半成品，与同计划中 `production_item` 相同的子工单匹配。
- 层级辅助事实：`Production Plan Sub Assembly Item.parent_item_code`、`production_item` 和 `bom_level`。
- 缺失关系：直接物料具有有效默认 BOM、按工厂规则应自产，但同计划没有活动子工单时，标记 `production_task_missing`，不得转成采购缺口。

同计划内执行顺序由最深 BOM 层级向顶层推进。跨订单竞争共享物料时，先按销售订单明细交期升序，再按计划开始时间、计划创建时间和单据名称稳定排序；没有交期的订单排在有明确交期的订单之后，并作为资料风险单独提示。

## 逐工单当前就绪度

就绪检查使用 `Work Order.required_items`，不再使用成品 BOM 完全展开结果。

每条直接物料需求使用：

```text
待发数量 = max(required_qty - transferred_qty, 0)
当前可分配 = 来源仓实际库存 - 销售及委外占用 - 已优先分配给更早需求的数量
即时缺口 = max(待发数量 - 当前可分配, 0)
```

生产工单和生产计划预留代表本生产体系自身的需求，不能在需求侧已经逐单分配后再次整体扣减，否则会双重计算。最终 Stock Entry 提交仍由 ERPNext 标准负库存校验兜底。

工单状态固定为：

- `ready_now`：全部待发直接物料已由当前实际库存覆盖。
- `materials_transferred`：所需物料已经全部转移，等待或正在生产。
- `in_progress`：工单已开始或已有产出。
- `waiting_subassembly`：即时缺口来自自产半成品，且存在未完成的下级工单。
- `purchase_shortage`：即时缺口来自外购物料，扣除可按期到达的 MR/PO 后仍需新采购。
- `awaiting_purchase_receipt`：当前库存不足，但已提交 PO 可按期覆盖。
- `purchase_request_pending`：当前库存不足，现有 MR 与 PO 合计可按期覆盖。
- `production_task_missing`：应自产的直接物料缺少对应生产任务。
- `blocked`：仓库、BOM、公司等基础资料无法完成计算。

只有 `ready_now` 才表示这张工单现在可以发料。MR、PO 和下级工单未来产出都不能把当前工单标记为可开工。

## 计划级生产摘要

生产工作台不再用顶层工单状态代表整个订单，而是返回并显示：

- 可立即开工工单数。
- 等待半成品工单数。
- 外购物料缺料工单数。
- 待采购到货工单数。
- 缺少生产任务或资料阻塞工单数。
- 已完成工单数和总工单数。

订单级主提示使用“可先开工 N 张工单”“等待半成品”“存在外购缺口”等可验证表达，不再把“底层物料齐套”等同于顶层工单可开工。

## Production Plan 采购缺口

采购规划面向完成整份活动 Production Plan 所需的全部外购物料，而不是只采购当前最底层工单的物料。

需求生成规则：

1. 读取计划下全部未完成 Work Order 的直接物料。
2. 对每行扣除已经转移的数量。
3. 如果物料按工厂规则为自产半成品，则该行是工单依赖，不进入采购需求；它的下级工单外购物料会单独进入汇总。
4. 只有明确允许采购且不属于计划内自产依赖的物料进入采购候选。
5. 按 `(item_code, source_warehouse)` 汇总候选，并保留 Production Plan、Work Order、Sales Order Item 和需要日期等来源说明。
6. 按订单交期优先分配共享现货，再按计划逐一分配可按期到达的 Purchase Order 和 Material Request 数量，禁止多个计划重复占用同一供应行。
7. 最终只把仍为 `new_purchase_required` 的数量交给一键采购。

“当前库存不足但已有未来供应”和“仍需新增采购”必须分别展示。一键采购不得为了让当前工单显示可开工而把在途数量当作现货。

## API 与模块边界

新增独立模块 `process_simplification/api/production_readiness.py`，负责：

- 查询订单行关联的活动 Production Plan 与 Work Order。
- 构建工单树和稳定排序。
- 将直接物料分类为自产依赖或外购候选。
- 按共享库存池计算逐工单当前就绪度。
- 生成计划级外购物料需求行。

现有模块职责调整：

- `production.py`：组合订单需求、计划摘要和工单树，负责生产工作台 API，不再自行展开成品 BOM 判断当前开工。
- `shortage.py`：保留库存快照、MR/PO 查询、供应分配和 Material Request 创建；增加对已归一化外购物料需求行的覆盖计算。
- `workbench.py`：订单履约数量继续按 Sales Order Item 计算，并补充 Production Plan 摘要和导航信息。
- `production_plan_adapter.py`：继续只负责调用标准 Production Plan 引擎创建和提交计划及工单，不承担动态就绪计算。

Quick Sales Order 在尚未创建 Production Plan 时仍属于下单前预测，可继续使用 BOM 展开做风险预览，但必须明确标记为“预计外购物料风险”，不能复用其结果声称某张工单当前可开工。

## 界面调整

### 订单工作台

- 显示活动 Production Plan 链接、计划数量和工单完成进度。
- “安排生产”仍进入生产工作台；已有计划时直接展开对应计划。
- 不直接在订单卡片上重复完整缺料表。

### 生产工作台

- 按订单行和 Production Plan 分组展示工单树。
- 每张工单显示生产物料、层级、来源仓、待发量、即时可用量、即时缺口和状态。
- 默认突出 `ready_now` 的最底层工单，并提供标准 Work Order / Stock Entry 导航。
- 计划级采购摘要与逐工单当前就绪度同时展示，但使用不同标签和颜色。

### 缺料采购

- 来源选择仍可从订单行进入，但后端必须解析为对应的活动 Production Plan。
- 结果来源显示 Production Plan、Work Order 和销售订单，不再只显示成品 BOM 来源。
- 没有生产计划的订单行提示“请先安排生产”，不生成推测性采购申请。

## 并发与一致性

- 工作台结果是实时计算快照，不创建库存预留。
- 创建 Material Request 前必须在服务器重新计算所选计划缺口，并拒绝超过最新缺口的数量，避免页面停留期间库存或在途供应变化导致重复采购。
- Stock Entry 提交继续作为最终库存一致性校验；工作台不能绕过负库存或仓库权限。
- 已取消 Production Plan、已取消/停止/关闭 Work Order 和已完成数量不进入活动需求。

## 测试与验收

### 后端单元测试

- 多层计划中只有最底层工单在直接原料有库存时为 `ready_now`。
- 顶层工单的自产半成品库存为零时为 `waiting_subassembly`，即使全部底层原料齐全也不能为 `ready_now`。
- 自产半成品缺少子工单时为 `production_task_missing`，不会进入采购缺口。
- 外购物料跨计划按交期只分配一次共享现货和一次 MR/PO 数量。
- PO/MR 覆盖只改变未来供应状态，不改变当前可开工状态。
- 已转移数量不再重复进入采购需求。

### 集成测试

- 真实两层 BOM 通过 Production Plan 创建成品和半成品工单后，工作台先显示半成品工单可开工、成品工单等待半成品。
- 半成品完成入库后重新计算，成品工单变为可开工。
- 一键采购只包含底层外购物料，不包含自产半成品。
- 创建采购申请前改变库存或新增 PO 时，服务器复核阻止超额采购。

### 前端测试

- 工单树层级、计划链接和各状态中文标签正确。
- “只看缺料”仅筛选需要新采购的外购物料；“当前可开工”筛选只返回 `ready_now` 工单。
- HTML 转义、标准路由和分页行为保持正确。

### 现场回归场景

对 `MFG-PP-2026-00005` 的六层工单链进行只读回归：初始状态应只允许最底层 `MFG-WO-2026-00036` 开工，顶层 `MFG-WO-2026-00031` 应显示等待“焊线线圈”，不得显示可立即发料。

## 非目标

- 不修改 Frappe 或 ERPNext 核心代码。
- 不自动提交 Stock Entry、Purchase Order 或 Purchase Receipt。
- 不把 Material Request/Purchase Order 原生 `sales_order_item` 当作共享采购来源字段。
- 不在本次重构中改变 Quick Sales Order 的下单流程。
- 不实现有限产能排程、工位排程或精确完工时间预测。
- 不引入新的持久化库存分配表；需要强占用时继续使用 ERPNext 标准库存预留能力。
