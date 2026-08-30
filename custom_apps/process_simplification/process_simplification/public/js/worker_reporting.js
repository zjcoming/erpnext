function workReportStatusMeta(status, translate = (message) => message) {
	const statuses = {
		"In Progress": { label: translate("计时中"), indicator: "blue" },
		"Pending Approval": { label: translate("待审核"), indicator: "orange" },
		Approved: { label: translate("已通过"), indicator: "green" },
		Rejected: { label: translate("已驳回"), indicator: "red" },
	};
	return statuses[status] || { label: status || translate("未知"), indicator: "gray" };
}

function workerExceptionStatusMeta(status, translate = (message) => message) {
	const statuses = {
		"Pending Review": { label: translate("待主管审核"), indicator: "orange" },
		"Pending Approval": { label: translate("待主管审核"), indicator: "orange" },
		Approved: { label: translate("已批准，待库存过账"), indicator: "blue" },
		"Awaiting Stock Entry": { label: translate("已批准，待库存过账"), indicator: "blue" },
		Applied: { label: translate("已登记损耗"), indicator: "green" },
		Completed: { label: translate("库存已过账"), indicator: "green" },
		Rejected: { label: translate("已驳回"), indicator: "red" },
	};
	return statuses[status] || { label: status || translate("未知"), indicator: "gray" };
}

function workerExceptionTypeLabel(requestType, translate = (message) => message) {
	const labels = {
		"Material Return": translate("退回未用物料"),
		"Material Scrap": translate("物料报废"),
		"Process Loss": translate("生产损耗"),
	};
	return labels[requestType] || requestType || translate("未知");
}

function workerExceptionMaterialOption(
	material,
	requestType,
	translate = (message) => message,
	formatNumber = (value) => String(value ?? "")
) {
	const code = String(material?.item_code || "").trim();
	const rawName = String(material?.item_name || "").trim();
	const displayName = rawName && rawName !== code ? rawName : translate("名称待维护");
	const target = requestType === "Material Scrap" ? material?.scrap_warehouse : material?.return_warehouse;
	return {
		label: displayName,
		value: material?.key || "",
		description: `${translate("物料编码")}：${code || translate("未设置")} · ${
			material?.source_warehouse || "-"
		} → ${target || "-"} · ${translate("可申请")} ${formatNumber(material?.requestable_qty)} ${
			material?.stock_uom || ""
		}`,
	};
}

function workReportButtonMeta(assignment, translate = (message) => message) {
	if (assignment.active_report) {
		return { action: "finish", label: translate("结束并报工"), disabled: false };
	}
	if (assignment.can_start) {
		return { action: "start", label: translate("开始计时"), disabled: false };
	}
	const labels = {
		PENDING_REPORT: translate("待主管审核"),
		NO_REMAINING_QTY: translate("已完成"),
		RATE_MISSING: translate("缺少计价规则"),
		DAILY_MINUTES_LIMIT: translate("今日工时已满"),
		MATERIAL_NOT_TRANSFERRED: translate("等待发料"),
		JOB_CARD_UNAVAILABLE: translate("不可报工"),
		TIME_LOG_SETTING: translate("配置不兼容"),
		PROCESS_LOSS: translate("需主管处理"),
	};
	return { action: null, label: labels[assignment.block_code] || translate("不可报工"), disabled: true };
}

function workReportTimerButtonMeta(assignment, translate = (message) => message) {
	if (!assignment?.active_report) return null;
	return assignment.timer_paused_at
		? { action: "resume", label: translate("继续计时"), disabled: false }
		: { action: "pause", label: translate("暂停计时"), disabled: false };
}

function workReportBlockMessage(assignment, translate = (message) => message) {
	const messages = {
		PENDING_REPORT: translate("已有报工等待主管审核，审核完成后才能继续。"),
		NO_REMAINING_QTY: translate("该生产任务单已没有可报工数量。"),
		RATE_MISSING: translate("当前日期没有有效计价规则，请联系主管。"),
		DAILY_MINUTES_LIMIT: translate("今日计薪分钟已达到上限。"),
		MATERIAL_NOT_TRANSFERRED: translate("物料尚未发放到在制仓，发料后才能开始报工。"),
	};
	return messages[assignment.block_code] || assignment.block_message || "";
}

function workReportAmount(wageType, qty, minutes, rate) {
	return wageType === "Time"
		? (Number(minutes || 0) / 60) * Number(rate || 0)
		: Number(qty || 0) * Number(rate || 0);
}

function workReportWageTypeLabel(wageType, translate = (message) => message) {
	return wageType === "Time" ? translate("计时") : translate("计件");
}

function workReportWageOptionLabel(
	option,
	format = (value) => String(value),
	translate = (message) => message
) {
	return option?.wage_type === "Time"
		? translate("计时 · {0}/小时", [format(option.rate)])
		: translate("计件 · {0}/件", [format(option?.rate)]);
}

function workReportWageOptions(assignment = {}) {
	const configured = Array.isArray(assignment.wage_options)
		? assignment.wage_options
		: assignment.wage_type
			? [{ wage_type: assignment.wage_type, rate: assignment.rate }]
			: [];
	const byType = new Map();
	for (const option of configured) {
		if (!["Piecework", "Time"].includes(option?.wage_type) || byType.has(option.wage_type)) continue;
		byType.set(option.wage_type, {
			wage_type: option.wage_type,
			rate: Number(option.rate || 0),
		});
	}
	return ["Piecework", "Time"].filter((wageType) => byType.has(wageType)).map(
		(wageType) => byType.get(wageType)
	);
}

function workReportDefaultWageType(assignment = {}) {
	const options = workReportWageOptions(assignment);
	return options.find((option) => option.wage_type === "Piecework")?.wage_type
		|| options[0]?.wage_type
		|| null;
}

function workReportRemainingMinutes(assignment) {
	return Math.max(
		0,
		Number(assignment.daily_minutes_limit || 0) - Number(assignment.daily_minutes_used || 0)
	);
}

function workReportWageLabel(
	assignment,
	format = (value) => String(value),
	translate = (message) => message
) {
	const options = workReportWageOptions(assignment);
	if (options.length > 1) {
		return options.map((option) => workReportWageOptionLabel(option, format, translate))
			.join(translate("；"));
	}
	if (assignment.active_report && assignment.wage_type) {
		return workReportWageOptionLabel(
			{ wage_type: assignment.wage_type, rate: assignment.rate },
			format,
			translate
		);
	}
	if (!options.length) return translate("未配置计价规则");
	const selectedType = assignment.wage_type || workReportDefaultWageType(assignment);
	const selected = options.find((option) => option.wage_type === selectedType) || options[0];
	return workReportWageOptionLabel(selected, format, translate);
}

function normalizeFrappeDateTime(value) {
	return typeof value === "string"
		? value.replace(/(\d{2}:\d{2}:\d{2})\.\d{1,6}(?=(?:Z|[+-]\d{2}:?\d{2})?$)/, "$1")
		: value;
}

function workReportFinishDialogStartedAt(assignment) {
	return normalizeFrappeDateTime(assignment?.active_started_at);
}

function runWorkerReportingToolbarLoad(load) {
	load();
}

function workerAssignmentPriority(assignment) {
	if (assignment?.active_report) return 0;
	if (assignment?.can_start) return 1;
	if (assignment?.block_code === "PENDING_REPORT") return 2;
	if (assignment?.block_code === "MATERIAL_NOT_TRANSFERRED") return 4;
	return 3;
}

function partitionWorkerAssignments(assignments = []) {
	const ordered = [...assignments].sort(
		(left, right) => workerAssignmentPriority(left) - workerAssignmentPriority(right)
	);
	return {
		active: ordered.filter((row) => row.active_report),
		ready: ordered.filter((row) => !row.active_report && row.can_start),
		blocked: ordered.filter(
			(row) =>
				!row.active_report &&
				!row.can_start &&
				row.block_code !== "MATERIAL_NOT_TRANSFERRED"
		),
		waitingMaterial: ordered.filter(
			(row) => !row.active_report && row.block_code === "MATERIAL_NOT_TRANSFERRED"
		),
	};
}

function shouldOpenWaitingMaterialGroup(partitions = {}) {
	return !partitions.active?.length && !partitions.ready?.length && !partitions.blocked?.length;
}

function workerAssignmentCardHtml(row, helpers = {}) {
	const translate = helpers.translate || ((message) => message);
	const escapeHtml = helpers.escapeHtml || ((value) => String(value ?? ""));
	const formatNumber = helpers.formatNumber || ((value) => String(value ?? ""));
	const formatDateTime = helpers.formatDateTime || ((value) => String(value || "-"));
	const button = workReportButtonMeta(row, translate);
	const timerButton = workReportTimerButtonMeta(row, translate);
	const blockMessage = workReportBlockMessage(row, translate);
	const wageLabel = workReportWageLabel(row, formatNumber, translate);
	const remainingMinutes = workReportRemainingMinutes(row);
	const stateClass = row.active_report
		? "is-active"
		: row.can_start
			? "is-ready"
			: row.block_code === "MATERIAL_NOT_TRANSFERRED"
				? "is-waiting-material"
				: "is-blocked";
	return `<article class="worker-assignment-card ${stateClass}" data-assignment="${escapeHtml(row.name)}">
		<div class="worker-assignment-heading"><strong>${escapeHtml(row.operation)}</strong><span>${escapeHtml(row.production_item || "")}</span></div>
		<div class="worker-assignment-facts">
			<span>${translate("生产任务单")}：${escapeHtml(row.job_card)}</span>
			<span>${translate("生产工单")}：${escapeHtml(row.work_order)}</span>
			<span>${translate("工作站")}：${escapeHtml(row.workstation || "-")}</span>
			<span>${translate("计价")}：${escapeHtml(wageLabel)}</span>
			<span>${translate("已通过/任务量")}：${formatNumber(row.completed_qty)} / ${formatNumber(row.for_quantity)}</span>
			<span>${translate("当前可报")}：${formatNumber(row.reportable_qty)}</span>
			${row.active_started_at ? `<span>${translate("开始时间")}：${escapeHtml(formatDateTime(row.active_started_at))}</span>` : ""}
			${row.active_report ? `<span>${translate("有效计时")}：${formatNumber(row.active_minutes)} ${translate("分钟")}${row.timer_paused_at ? ` · ${translate("已暂停")}` : ""}</span>` : ""}
			${row.wage_type === "Time" ? `<span>${translate("今日剩余计薪分钟")}：${formatNumber(remainingMinutes)}</span>` : ""}
			${row.wage_type === "Time" ? `<span>${translate("计薪工时来源")}：${row.manual_time_entry ? translate("工人手工填写") : translate("计时器有效分钟")}</span>` : ""}
		</div>
		${row.notes ? `<p class="text-muted worker-assignment-note">${escapeHtml(row.notes)}</p>` : ""}
		${blockMessage ? `<p class="text-muted worker-assignment-block-message">${escapeHtml(blockMessage)}</p>` : ""}
		<div class="worker-assignment-actions">
			<button class="btn btn-primary worker-report-action" data-assignment="${escapeHtml(row.name)}" ${button.disabled ? "disabled" : ""}>${escapeHtml(button.label)}</button>
			${timerButton ? `<button class="btn btn-default worker-timer-action" data-assignment="${escapeHtml(row.name)}" data-action="${escapeHtml(timerButton.action)}">${escapeHtml(timerButton.label)}</button>` : ""}
			<button class="btn btn-default worker-exception-action" data-assignment="${escapeHtml(row.name)}">${translate("申请退料/报废")}</button>
		</div>
	</article>`;
}

function mountWorkerReportingPage({ page, root, mode = "queue" }) {
	const $root = root;
	const state = { data: { assignments: [], reports: [] } };
	const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
	const number = (value) => format_number(flt(value), null, 2);
	const dateTime = (value) =>
		value ? frappe.datetime.str_to_user(normalizeFrappeDateTime(value)) : "-";
	const helpers = {
		translate: __,
		escapeHtml: esc,
		formatNumber: number,
		formatDateTime: dateTime,
	};

	function renderSummary(partitions) {
		const focus = mode === "active"
			? `${__("正在做")} ${partitions.active.length} ${__("项")}`
			: `${__("待处理派工")} ${
				partitions.ready.length + partitions.blocked.length + partitions.waitingMaterial.length
			} ${__("项")}`;
		$root.find(".worker-reporting-summary").html(
			`<div><strong>${esc(focus)}</strong><span>${__("今日计薪分钟")}：${number(state.data.daily_minutes_used)} / ${number(state.data.daily_minutes_limit)}</span></div>`
		);
	}

	function cardGrid(rows) {
		return `<div class="worker-assignment-grid">${rows
			.map((row) => workerAssignmentCardHtml(row, helpers))
			.join("")}</div>`;
	}

	function renderQueue(partitions) {
		$root.find(".worker-active-shortcut").html(
			partitions.active.length
				? `<button class="worker-focus-banner worker-open-active" type="button"><span><strong>${__("正在做")} ${partitions.active.length} ${__("项")}</strong><small>${__("优先处理计时、暂停或结束报工")}</small></span><span>${__("立即查看")} →</span></button>`
				: ""
		);

		const groups = [];
		if (partitions.ready.length) {
			groups.push(`<section class="worker-assignment-group is-ready-group"><h5>${__("可以开始")} <span>${partitions.ready.length}</span></h5>${cardGrid(partitions.ready)}</section>`);
		}
		if (partitions.blocked.length) {
			groups.push(`<section class="worker-assignment-group"><h5>${__("待处理")} <span>${partitions.blocked.length}</span></h5>${cardGrid(partitions.blocked)}</section>`);
		}
		if (partitions.waitingMaterial.length) {
			const shouldOpen = shouldOpenWaitingMaterialGroup(partitions);
			groups.push(`<details class="worker-assignment-group worker-waiting-group" ${shouldOpen ? "open" : ""}><summary>${__("等待发料")} <span>${partitions.waitingMaterial.length}</span><small>${__("发料后才可开始")}</small></summary>${cardGrid(partitions.waitingMaterial)}</details>`);
		}
		$root.find(".worker-assignment-list").html(
			groups.join("") || `<div class="text-muted worker-reporting-empty">${__("当前没有待开始的派工任务。")}</div>`
		);
	}

	function renderActive(partitions) {
		$root.find(".worker-active-list").html(
			partitions.active.length
				? cardGrid(partitions.active)
				: `<div class="worker-reporting-empty worker-active-empty"><strong>${__("当前没有正在做的任务")}</strong><span class="text-muted">${__("从“我的报工”选择派工并开始计时后，会显示在这里。")}</span><button class="btn btn-primary worker-open-queue">${__("查看当前派工")}</button></div>`
		);
	}

	function render() {
		const partitions = partitionWorkerAssignments(state.data.assignments || []);
		renderSummary(partitions);
		if (mode === "active") renderActive(partitions);
		else renderQueue(partitions);
	}

	function load() {
		return frappe.call({
			method: "process_simplification.api.production_reporting.get_my_dashboard",
			freeze: true,
			freeze_message: mode === "active" ? __("正在读取进行中的任务...") : __("正在读取当前派工..."),
		}).then((response) => {
			state.data = response.message || { assignments: [], reports: [] };
			render();
		});
	}

	async function beginWork(row) {
		const requestId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
		await frappe.call({
			method: "process_simplification.api.production_reporting.start_work_session",
			type: "POST",
			freeze: true,
			freeze_message: __("正在开始计时..."),
			args: { assignment: row.name, request_id: requestId },
		});
		frappe.show_alert({ message: __("已经开始计时。"), indicator: "green" });
		frappe.set_route("active-production-work");
	}

	function startWork(row) {
		return beginWork(row);
	}

	async function changeTimerState(row, action) {
		const requestId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
		await frappe.call({
			method: `process_simplification.api.production_reporting.${action}_work_session`,
			type: "POST",
			freeze: true,
			freeze_message: action === "pause" ? __("正在暂停计时...") : __("正在继续计时..."),
			args: { report: row.active_report, request_id: requestId },
		});
		frappe.show_alert({
			message: action === "pause" ? __("计时已暂停。") : __("已继续计时。"),
			indicator: action === "pause" ? "orange" : "green",
		});
		await load();
	}

	function openFinishDialog(row) {
		const requestId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
		const wageOptions = workReportWageOptions(row);
		const defaultWageType = workReportDefaultWageType(row);
		const labelToWageType = new Map(
			wageOptions.map((option) => [workReportWageOptionLabel(option, number, __), option.wage_type])
		);
		const defaultOption = wageOptions.find(
			(option) => option.wage_type === defaultWageType
		) || wageOptions[0];
		const defaultOptionLabel = defaultOption
			? workReportWageOptionLabel(defaultOption, number, __)
			: "";
		const canEnterTimeMinutes = Boolean(row.time_manual_entry_available);
		const hasTimeOption = wageOptions.some((option) => option.wage_type === "Time");
		let dialog;

		function selectedWageType() {
			if (wageOptions.length <= 1) return wageOptions[0]?.wage_type || row.wage_type;
			return labelToWageType.get(dialog?.get_value("wage_type")) || null;
		}

		function wageNote(wageType) {
			if (wageType === "Time" && canEnterTimeMinutes) {
				return __("计时器只累计未暂停的实际生产分钟；计薪分钟由工人填写，并与实际分钟一起交主管审核。");
			}
			if (wageType === "Time") {
				return __("计时工资使用服务器累计的有效计时分钟；暂停期间不计入。跨零点的夜班仍归入开始计时当天。");
			}
			return __("计件工资按本次完成数量和计件单价计算；计时器记录仍会随报工提交主管审核。");
		}

		function refreshWageFields() {
			const wageType = selectedWageType();
			const showReportedMinutes = wageType === "Time" && canEnterTimeMinutes;
			if (dialog.fields_dict.reported_minutes) {
				dialog.set_df_property("reported_minutes", "hidden", !showReportedMinutes);
				dialog.set_df_property("reported_minutes", "reqd", showReportedMinutes);
				if (!showReportedMinutes) dialog.set_value("reported_minutes", null);
			}
			if (dialog.fields_dict.time_note) {
				dialog.fields_dict.time_note.$wrapper.html(
					`<p class="text-muted">${wageNote(wageType)}</p>`
				);
			}
		}

		const fields = [
			{ fieldname: "job_card", fieldtype: "Data", label: __("Job Card"), read_only: 1, default: row.job_card },
			{ fieldname: "operation", fieldtype: "Data", label: __("工序"), read_only: 1, default: row.operation },
			{ fieldname: "started_at", fieldtype: "Datetime", label: __("实际开始时间"), read_only: 1, default: workReportFinishDialogStartedAt(row) },
			{ fieldname: "captured_minutes", fieldtype: "Float", label: __("当前有效计时分钟"), read_only: 1, default: row.active_minutes },
		];
		if (wageOptions.length > 1) {
			fields.push({
				fieldname: "wage_type",
				fieldtype: "Select",
				label: __("计价方式与单价"),
				options: [...labelToWageType.keys()].join("\n"),
				default: defaultOptionLabel,
				reqd: 1,
				onchange: refreshWageFields,
			});
		} else {
			fields.push({
				fieldname: "wage",
				fieldtype: "Data",
				label: __("计价方式与单价"),
				read_only: 1,
				default: defaultOptionLabel,
			});
		}
		fields.push(
			{ fieldname: "remaining", fieldtype: "Float", label: __("当前可报数量"), read_only: 1, default: row.reportable_qty },
			{ fieldname: "completed_qty", fieldtype: "Float", label: __("本次完成数量"), reqd: 1 },
		);
		if (hasTimeOption && canEnterTimeMinutes) {
			fields.push({
				fieldname: "reported_minutes",
				fieldtype: "Float",
				label: __("计薪分钟（工人填写）"),
				hidden: defaultWageType !== "Time",
				reqd: defaultWageType === "Time",
			});
		}
		fields.push({
			fieldname: "time_note",
			fieldtype: "HTML",
			options: `<p class="text-muted">${wageNote(defaultWageType)}</p>`,
		});
		dialog = new frappe.ui.Dialog({
			title: __("结束计时并提交报工"),
			fields,
			primary_action_label: __("提交主管审核"),
			primary_action: async (values) => {
				const wageType = selectedWageType();
				if (!wageType) {
					frappe.msgprint(__("请选择有效的计价方式。"));
					return;
				}
				if (flt(values.completed_qty) <= 0 || flt(values.completed_qty) > flt(row.reportable_qty)) {
					frappe.msgprint(__("本次完成数量必须大于 0 且不超过当前可报数量。"));
					return;
				}
				if (
					wageType === "Time"
					&& canEnterTimeMinutes
					&& flt(values.reported_minutes) <= 0
				) {
					frappe.msgprint(__("计时计价时，请填写大于 0 的计薪分钟。"));
					return;
				}
				const $button = dialog.get_primary_btn();
				$button.prop("disabled", true).text(__("提交中..."));
				try {
					await frappe.call({
						method: "process_simplification.api.production_reporting.finish_work_session",
						type: "POST",
						args: {
							report: row.active_report,
							completed_qty: values.completed_qty,
							reported_minutes: values.reported_minutes,
							wage_type: wageType,
							request_id: requestId,
						},
					});
					dialog.hide();
					frappe.show_alert({ message: __("报工已提交主管审核。"), indicator: "green" });
					await load();
				} finally {
					$button.prop("disabled", false).text(__("提交主管审核"));
				}
			},
		});
		dialog.set_secondary_action(() => {
			frappe.confirm(__("确定取消本次计时吗？未提交的计时记录将被删除。"), async () => {
				await frappe.call({
					method: "process_simplification.api.production_reporting.cancel_work_session",
					type: "POST",
					args: { report: row.active_report },
				});
				dialog.hide();
				frappe.show_alert({ message: __("本次计时已取消。"), indicator: "orange" });
				await load();
			});
		});
		dialog.set_secondary_action_label(__("取消本次计时"));
		dialog.show();
		if (wageOptions.length > 1) dialog.set_value("wage_type", defaultOptionLabel);
		refreshWageFields();
		if (!row.can_finish) dialog.get_primary_btn().prop("disabled", true);
	}

	async function openExceptionDialog(row) {
		const response = await frappe.call({
			method: "process_simplification.api.production_exceptions.get_exception_options",
			args: { assignment: row.name },
			freeze: true,
			freeze_message: __("正在核对可退物料和可登记损耗数量..."),
		});
		const options = response.message || { materials: [], process_loss_available_qty: 0 };
		const typeLabels = {
			[__("退回未用物料")]: "Material Return",
			[__("物料报废")]: "Material Scrap",
			[__("生产损耗")]: "Process Loss",
		};
		const causeLabels = {
			[__("来料/物料异常")]: "Material Defect",
			[__("操作失误")]: "Operation Error",
			[__("其他")]: "Other",
		};
		let materialChoices = new Map();
		let dialog;

		function selectedType() {
			return typeLabels[dialog.get_value("request_type")] || "";
		}

		function refreshMaterialChoices() {
			const requestType = selectedType();
			const materialRows = (options.materials || []).filter(
				(material) => requestType !== "Material Scrap" || material.scrap_warehouse
			);
			materialChoices = new Map(materialRows.map((material) => [material.key, material]));
			const autocompleteOptions = materialRows.map((material) =>
				workerExceptionMaterialOption(material, requestType, __, number)
			);
			const isMaterial = requestType === "Material Return" || requestType === "Material Scrap";
			dialog.set_df_property("material_choice", "hidden", !isMaterial);
			dialog.set_df_property("material_choice", "reqd", isMaterial);
			dialog.fields_dict.material_choice.set_data(autocompleteOptions);
			dialog.set_df_property("material_choice", "description", "");
			dialog.set_value("material_choice", "");
			dialog.set_value("available_qty", isMaterial ? 0 : options.process_loss_available_qty);
		}

		function refreshAvailableQty() {
			const requestType = selectedType();
			const material = materialChoices.get(dialog.get_value("material_choice"));
			dialog.set_df_property(
				"material_choice",
				"description",
				material ? `${__("物料编码")}：${material.item_code}` : ""
			);
			dialog.set_value(
				"available_qty",
				requestType === "Process Loss"
					? options.process_loss_available_qty
					: material?.requestable_qty || 0
			);
		}

		dialog = new frappe.ui.Dialog({
			title: __("申请退料/报废"),
			fields: [
				{ fieldname: "job_card", fieldtype: "Data", label: __("Job Card"), read_only: 1, default: options.job_card },
				{ fieldname: "operation", fieldtype: "Data", label: __("工序"), read_only: 1, default: options.operation },
				{ fieldname: "request_type", fieldtype: "Select", label: __("申请类型"), reqd: 1, options: Object.keys(typeLabels), onchange: refreshMaterialChoices },
				{ fieldname: "cause", fieldtype: "Select", label: __("原因类别"), reqd: 1, options: Object.keys(causeLabels) },
				{ fieldname: "material_choice", fieldtype: "Autocomplete", label: __("物料与仓库路径"), reqd: 1, options: [], onchange: refreshAvailableQty },
				{ fieldname: "available_qty", fieldtype: "Float", label: __("当前最多可申请数量"), read_only: 1 },
				{ fieldname: "qty", fieldtype: "Float", label: __("申请数量"), reqd: 1 },
				{ fieldname: "reason", fieldtype: "Small Text", label: __("情况说明"), reqd: 1 },
				{
					fieldname: "posting_note",
					fieldtype: "HTML",
					options: `<p class="text-muted">${__("主管批准退料或物料报废后，系统只生成原生库存移动草稿；库存人员提交单据后才会改变库存。生产损耗经批准后计入 Job Card，但不计入工人工资数量。")}</p>`,
				},
			],
			primary_action_label: __("提交主管审核"),
			primary_action: async (values) => {
				const requestType = typeLabels[values.request_type];
				const material = materialChoices.get(values.material_choice);
				const maximum = requestType === "Process Loss"
					? options.process_loss_available_qty
					: material?.requestable_qty || 0;
				if (!requestType || !causeLabels[values.cause]) {
					frappe.msgprint(__("请选择有效的申请类型和原因类别。"));
					return;
				}
				if (requestType !== "Process Loss" && !material) {
					frappe.msgprint(__("当前没有可供该类型申请的物料，或尚未配置报废仓。"));
					return;
				}
				if (flt(values.qty) <= 0 || flt(values.qty) > flt(maximum)) {
					frappe.msgprint(__("申请数量必须大于 0 且不超过当前最多可申请数量。"));
					return;
				}
				const $button = dialog.get_primary_btn();
				$button.prop("disabled", true).text(__("提交中..."));
				try {
					await frappe.call({
						method: "process_simplification.api.production_exceptions.submit_exception",
						type: "POST",
						args: {
							assignment: row.name,
							request_type: requestType,
							qty: values.qty,
							cause: causeLabels[values.cause],
							reason: values.reason,
							request_key: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`,
							material_key: material?.key,
						},
					});
					dialog.hide();
					frappe.show_alert({ message: __("异常申请已提交主管审核。"), indicator: "green" });
					await load();
				} finally {
					$button.prop("disabled", false).text(__("提交主管审核"));
				}
			},
		});
		dialog.show();
		dialog.set_value("request_type", Object.keys(typeLabels)[0]);
		dialog.set_value("cause", Object.keys(causeLabels)[0]);
		refreshMaterialChoices();
	}

	$root.on("click", ".worker-report-action", (event) => {
		const row = (state.data.assignments || []).find(
			(item) => item.name === $(event.currentTarget).data("assignment")
		);
		const action = row ? workReportButtonMeta(row, __).action : null;
		if (action === "start") startWork(row);
		if (action === "finish") openFinishDialog(row);
	});
	$root.on("click", ".worker-timer-action", (event) => {
		const row = (state.data.assignments || []).find(
			(item) => item.name === $(event.currentTarget).data("assignment")
		);
		const action = $(event.currentTarget).data("action");
		if (row && ["pause", "resume"].includes(action)) changeTimerState(row, action);
	});
	$root.on("click", ".worker-exception-action", (event) => {
		const row = (state.data.assignments || []).find(
			(item) => item.name === $(event.currentTarget).data("assignment")
		);
		if (row) openExceptionDialog(row);
	});
	$root.on("click", ".worker-open-active", () => frappe.set_route("active-production-work"));
	$root.on("click", ".worker-open-queue", () => frappe.set_route("my-production-reporting"));
	page.add_inner_button(__("刷新"), () => runWorkerReportingToolbarLoad(load));
	page.worker_reporting = { state, load };
	return page.worker_reporting;
}

const workerReportingApi = {
	workReportStatusMeta,
	workerExceptionStatusMeta,
	workerExceptionTypeLabel,
	workerExceptionMaterialOption,
	workReportButtonMeta,
	workReportTimerButtonMeta,
	workReportBlockMessage,
	workReportAmount,
	workReportWageTypeLabel,
	workReportWageOptionLabel,
	workReportWageOptions,
	workReportDefaultWageType,
	workReportRemainingMinutes,
	workReportWageLabel,
	normalizeFrappeDateTime,
	workReportFinishDialogStartedAt,
	runWorkerReportingToolbarLoad,
	workerAssignmentPriority,
	partitionWorkerAssignments,
	shouldOpenWaitingMaterialGroup,
	workerAssignmentCardHtml,
	mountWorkerReportingPage,
};

if (typeof module !== "undefined" && module.exports) module.exports = workerReportingApi;
if (typeof window !== "undefined") {
	window.process_simplification = window.process_simplification || {};
	window.process_simplification.worker_reporting = workerReportingApi;
}
