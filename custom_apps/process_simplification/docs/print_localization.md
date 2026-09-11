# 打印中文默认行为

最新版本已扩展五类默认打印与库存扫码，安装 / 升级和后台 PDF 行为以 [工厂默认打印与库存扫码](factory_print_suite.md) 为准。以下保留此前实现及验证记录。

进入 Desk 打印页时默认使用 `zh`，覆盖原生“单据语言 → 模板默认语言 → 用户语言”的初始选择。页面显示“中文”，仍向预览、全屏和 PDF 接口传递正确的语言 ID。用户手动选择其他语言后，在本次预览中切换格式或刷新会保留选择；再次进入打印页恢复中文。不会修改单据的语言字段、用户语言或模板记录。

## 采购模板

`Purchase Order Standard` 和 `Purchase Order with Item Image` 在选择器中显示中文名称，后台格式 ID 保持不变。只对这两种原生标准模板的中文渲染进行处理：

- 单号、小计、合计、金额大写等缺失翻译补齐；行号改用“序号”，避免原生 `No` 被翻译成“否”。
- 单位通过翻译函数显示，补齐个 / 克 / 米，不修改物料或 UOM 主数据。
- CNY 金额大写从模板实际展示的 `grand_total` 计算，按元角分表达；外币保留原大写。不会改动金额、舍入总额或已保存的 `in_words`。
- 英文打印保持原生模板行为，其他用户自定义模板不做字符串转换。`CNY` 货币代码、型号和单号保留。

实现使用本应用 `page_js`、`get_print_format_template`、Jinja helper 和中文 PO 翻译文件，无 ERPNext / Frappe 核心代码改动。

## 公司资料弹窗

ERPNext 原生打印页在商业模板或原生公司表头下，会自动读取 `get_missing_company_details`。公司 Logo、电话、邮箱、公司关联地址或单据公司地址缺失时，会弹出资料补全窗口，网站等空字段也被要求填写。

本应用通过 `override_whitelisted_methods` 让这一自动检查返回空结果，取消进入打印页时的资料向导。Company 和 Address 正常编辑入口及权限保留；表头继续读取当前单据所属公司已有资料，空项不打印。

## 开发站点验证

2026-09-11，`development.localhost` 的 `PUR-ORD-2026-00010` 实际语言字段为空，两种原生采购模板的默认语言均为 `en`；公司缺少 Logo、网站、电话、邮箱及地址。

已验证两种模板的中英文 HTML、中文原生 wkhtmltopdf PDF、浏览器首次进入中文及工厂表头、无资料弹窗、手动英文打印。该单中文金额大写为“人民币玖万柒仟陆佰柒拾壹元捌角”，采购订单和 Company 前后内容一致。标准版 PDF 为 1 页，含图片版按原模板分页为 2 页。

相关新增测试：7 项 Python、7 项 JavaScript，覆盖元角分及舍入、模板和语言边界、HTML 转义、默认选择、语言手动选择保留、中文名称与 ID 映射、自定义表头保留。

验证文件位于工作区忽略目录 `outputs/print-letterhead-20260911/print-localization-checks.json`、`localized-po-*-zh.pdf`。尚未发布云端。

## 更新与缓存

发布时按正常应用流程迁移、构建并清缓存，确保中文 PO 文件编译成 MO。仅在开发站点更新 page hook 而不迁移时，还需执行 `bench --site <site> execute frappe.cache_manager.reset_metadata_version` 和 `clear-cache`，使已打开浏览器在刷新后丢弃旧的打印页脚本。
