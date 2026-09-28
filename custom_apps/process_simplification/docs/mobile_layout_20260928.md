# 手机布局优化（2026-09-28）

本次调整集中在自定义应用，未修改 Frappe / ERPNext 核心或业务数据逻辑。

- 767px 及以下：页头标题与操作分行；扫码保留可访问名称并改为图标按钮；主操作允许收缩和换行；菜单限制宽高并支持内部滚动。
- 查询报表：手机上采用两列筛选，显示字段名称。必填字段与已有值的条件始终可见，空的可选条件通过“更多筛选”展开。无必填字段的报表保留前三个可见条件，已有原生折叠功能的报表不接管其折叠行为。
- 缺料采购：仅物料标题保留复选框缩进，其余信息使用整张卡片宽度；缩小重复留白，数量与缺口优先显示；359px 及以下数量输入独占一行。
- 原生表单：调整手机上的字段间距；表格继续使用原有的内部横向滚动。
- 768px 以上：保留桌面页头、筛选与采购表格布局。

## 验证

以下为发布前在本地隔离演示站点 `returns-demo.localhost`（端口 18090）完成的验证。生产发布与线上复核见下文。

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

## 生产发布

2026-09-28 15:41:45（北京时间）完成部署，站点为 `https://erp.hengsuankeji.com`。

- 分支：`rc/develop-v16`；应用提交：`6ed984be3ddb347fd0450294954a791ed21ab682`。
- 基础镜像：`hengsuan/erpnext:v16.33.0-hs.20260928.1`，包含当日老板权限修复。
- 发布镜像：`hengsuan/erpnext:v16.33.0-hs.20260928.2`。
- 镜像 ID：`sha256:72d287dd5303b1b139b3fd7c5971240c6368c178752788b924cda74db19d3ea0`。
- 完整应用源码指纹：374 个文件，`651a2416f6dcd516e6b83b5bccfef8fbae41af794d3b77d24746c835748f2422`，与固定 Git 提交一致。

基于已验证生产镜像构建新镜像，覆盖上述提交中的 hooks、两份 CSS、新增 JS 及其测试。五个文件下载后逐一校验 SHA-256；镜像内静态资源链接指向应用 public 目录，直接提供本次未打包的 CSS/JS。同步刷新 `assets/assets.json` 的修改时间，使 Frappe 页面缓存随构建版本失效；清理站点缓存后重建六个应用服务，未执行数据库迁移。Frappe 16.32.0、ERPNext 16.33.0、已有补丁与 3×4 Web 并发保持不变。

## 生产验证

- 候选镜像在无网络环境运行相关 JavaScript 测试，20 项全部通过。
- 六个应用服务使用同一新镜像，backend/frontend 健康；维护模式、开发模式、测试开关均关闭。
- 公网 HTTPS ping 返回 200 / `pong`；服务恢复后再连续检查三次本机 ping 均正常。
- 浏览器实际加载 `process_ui.css?v=17`、`mobile_desk.css?v=3`、`mobile_desk.js?v=2`，三个公网资源的 SHA-256 均与提交一致。
- 线上库存流水在 390px 视口下页面宽度为 390px，默认筛选区高 280px；实际展开、收起均正常。
- 线上缺料采购显示现有 10 种物料，390px 与 320px 均无页面横向溢出，主操作按钮完整可见；390px 下首张卡片高约 350px。
- 现有采购订单 `PUR-ORD-2026-00005` 在 320px 视口下页面没有横向溢出；菜单宽 240px、高 612px，内部内容高 1003px，实际滚动至底部通过。

以上为 Chrome 手机尺寸模拟，未做手机实机验收；生产浏览器验证未生成采购申请或修改业务单据。

## 备份与回退

维护期间停止前端、队列和调度器，保存数据库、公开附件、私有附件及站点配置，压缩包完整性和四份文件的 SHA-256 校验通过。在业务服务停止期间，切换代码及清理缓存前后，28 张业务、账号与权限表的逐行摘要完全一致。`.env` 仅替换镜像标签，Compose 文件保持一致。

服务器受限证据目录：`/opt/hengsuan-erp/releases/20260928.2-mobile/`。其中 `backup/`、`backup-sha256.txt`、`data-before.json`、`data-after.json`、`candidate-gate.txt`、`source-gate.txt`、`js-tests.txt`、`image-id.txt`、`deploy.log`、`completed.txt` 及原配置均已保留，未提交 Git。

部署脚本包含失败时恢复上一版 `.env`、清理旧版缓存、退出维护并重启服务的回退逻辑。本次未触发回退，未恢复数据库，也未执行异地备份复制或恢复演练。刷新已有页面即可加载新版布局。
