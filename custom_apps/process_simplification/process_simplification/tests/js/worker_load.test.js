const test = require("node:test");
const assert = require("node:assert/strict");
const { createWorkerLoadController, workerLoadPreviewHtml } = require("../../public/js/worker_assignment.js");
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

test("changing employees cannot display the previous employee's delayed workload", async () => {
	const first = deferred(); const second = deferred(); const output = [];
	const controller = createWorkerLoadController({ read: (_, names) => names[0] === "A" ? first.promise : second.promise, render: (...args) => output.push(args) });
	const a = controller.load({ jobCard: "JOB", employees: ["A"] });
	const b = controller.load({ jobCard: "JOB", employees: ["B"] });
	second.resolve({ employees: [{ employee: "B", active_count: 1 }] }); await b;
	first.resolve({ employees: [{ employee: "A" }] }); await a;
	assert.deepEqual(output.filter(([status]) => status === "ready").map(([, data]) => data.employees[0].employee), ["B"]);
});

test("quantity-only changes reuse load; manual refresh bypasses it; failures permit retry", async () => {
	let calls = 0; let fail = false; const statuses = [];
	const controller = createWorkerLoadController({ read: async () => { calls++; if (fail) throw Error("offline"); return { employees: [] }; }, render: (state) => statuses.push(state) });
	const selection = { jobCard: "JOB", employees: ["B", "A", "A"] };
	await controller.load(selection); await controller.load(selection);
	assert.equal(calls, 1);
	fail = true; await controller.load(selection, true); assert.equal(statuses.at(-1), "error");
	fail = false; await controller.load(selection); assert.equal(statuses.at(-1), "ready");
	assert.equal(calls, 3);
});

test("closing dialog invalidates pending replies and no polling is scheduled", async () => {
	const pending = deferred(); const statuses = [];
	const controller = createWorkerLoadController({ read: () => pending.promise, render: (state) => statuses.push(state) });
	const promise = controller.load({ jobCard: "JOB", employees: ["A"] }); controller.dispose();
	pending.resolve({ employees: [{ employee: "A" }] }); await promise;
	assert.deepEqual(statuses, ["loading"]);
});

test("inaccessible employee is unknown instead of falsely idle", async () => {
	let result;
	const controller = createWorkerLoadController({ read: async () => ({ employees: [] }), render: (state, model) => { if (state === "ready") result = model; } });
	await controller.load({ jobCard: "JOB", employees: ["HIDDEN"] });
	assert.deepEqual(result.unavailable, ["HIDDEN"]);
});

test("workload shows name, paused session and separate fractional quantities without wage data", () => {
	const html = workerLoadPreviewHtml({ checked_at: "2026-09-10 12:00", employees: [{ employee: "EMP", employee_name: "张师傅", active_count: 1, queued_count: 2, pending_review_count: 1, jobs: [{ state: "active", paused: true, operation: "绕线", remaining_qty: 4.5, stock_uom: "Meter", job_card: "JOB", work_order: "WO", is_current: true }] }] }, { escapeHtml: (s) => String(s).replaceAll("<", "&lt;"), translate: (s) => s, formatNumber: (n) => String(n) });
	assert.match(html, /张师傅/); assert.match(html, /计时已暂停/); assert.match(html, /4.5 Meter/);
	assert.match(html, /本次工序/); assert.match(html, /待做含待料/); assert.doesNotMatch(html, /工资|计薪/);
});
