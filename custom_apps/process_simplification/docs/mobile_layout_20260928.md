# 手机布局优化（2026-09-28）

本次调整集中在自定义应用，未修改 Frappe / ERPNext 核心或业务数据逻辑。

- 767px 及以下：页头标题与操作分行；扫码保留可访问名称并改为图标按钮；主操作允许收缩和换行；菜单限制宽高并支持内部滚动。
- 查询报表：手机上采用两列筛选，显示字段名称。必填字段与已有值的条件始终可见，空的可选条件通过“更多筛选”展开。无必填字段的报表保留前三个可见条件，已有原生折叠功能的报表不接管其折叠行为。
- 缺料采购：仅物料标题保留复选框缩进，其余信息使用整张卡片宽度；缩小重复留白，数量与缺口优先显示；359px 及以下数量输入独占一行。
- 原生表单：调整手机上的字段间距；表格继续使用原有的内部横向滚动。
- 768px 以上：保留桌面页头、筛选与采购表格布局。

## 验证

在本地隔离演示站点 `returns-demo.localhost`（端口 18090）完成，未部署到线上。

| 检查 | 结果 |
| --- | --- |
| 库存流水，320px / 390px | 页面无横向溢出；默认筛选区 280px 高 |
| 筛选交互 | 可展开、收起；已填凭证号收起后仍可见 |
| 库存流水切换库存余额 | 异步重建后只有一个折叠按钮 |
| 未保存采购订单，320px | 保存按钮完整；菜单宽 288px，高 612px，内容内部滚动 |
| 库存流水，768px | 页面宽度仍为 768px，报表未撑宽页面 |
| 桌面，1440px | 折叠按钮隐藏；原生筛选、49px 页头和采购表格布局保留 |
| 缺料卡片布局样例，320px / 390px | 无页面溢出；长物料名、仓库名换行；窄屏长数量输入有足够空间 |

演示库没有待采购缺料，因此卡片使用实际 `shortagePageHtml` / `shortageRowsHtml` 渲染函数及应用样式生成布局样例，未生成采购申请或测试库存单。样例不替代采购业务端到端验收。

检查命令：

```sh
node --check custom_apps/process_simplification/process_simplification/public/js/mobile_desk.js
node --test custom_apps/process_simplification/process_simplification/tests/js/mobile_desk.test.js custom_apps/process_simplification/process_simplification/tests/js/document_scan_entry.test.js custom_apps/process_simplification/process_simplification/tests/js/shortage_purchase_planning.test.js custom_apps/process_simplification/process_simplification/tests/js/shortage_purchase_planning_navigation.test.js
git diff --check
```

20 项测试通过。隔离 Bench 的 `bench build --app process_simplification` 与站点 `clear-cache` 完成。发布时需同步新增 JS/CSS 与 hooks、构建资源并清理站点缓存；hooks 中已更新资源版本号。
