const test = require("node:test");
const assert = require("node:assert/strict");
const worker = require("../../public/js/worker_reporting.js");
const planned = require("../../public/js/worker_prearrangement.js");

test("inactive tasks stay accessible in a collapsed group alongside executable work", () => {
	const oldTask = { name: "OLD", job_card: "JC-OLD", block_code: "WORK_ORDER_UNAVAILABLE" };
	const ready = { name: "READY", job_card: "JC-READY", can_start: true };
	const plan = { name: "PLAN", job_card: "JC-PLAN", block_code: "PRODUCTION_PLAN_UNAVAILABLE" };
	const queue = worker.buildWorkerTaskQueue([oldTask, ready], { tasks: [plan] });
	assert.equal(queue.groups.ready.length, 1);
	assert.equal(queue.groups.attention.length, 0);
	assert.equal(queue.groups.unavailable.length, 2);
	assert.equal(queue.total, 3);
	const html = worker.workerTaskQueueHtml(queue, { plannedCard: planned.workerPlannedCardHtml });
	assert.match(html, /<details[^>]+data-task-group="unavailable">/);
	assert.doesNotMatch(html, /<details[^>]+data-task-group="unavailable"[^>]*\bopen/);
	assert.match(html, /data-assignment="OLD"/);
	assert.match(html, /暂不可执行/);
	assert.deepEqual(planned.workerPlannedStatus(plan), { label: "暂不可执行", indicator: "gray" });
});

test("active timers, pending reports, and uncertain starts are never folded away", () => {
	const base = { name: "A", job_card: "JC-A", block_code: "WORK_ORDER_UNAVAILABLE" };
	const active = worker.buildWorkerTaskQueue([{ ...base, active_report: "REPORT" }]);
	assert.equal(active.active.length, 1);
	assert.equal(active.groups.unavailable.length, 0);
	for (const pending of [{ has_pending_report: true }, { pending_qty: 1 }]) {
		assert.equal(worker.workerQueueGroup({ ...base, ...pending }), "attention");
	}
	for (const status of ["starting", "uncertain", "checking"]) {
		const queue = worker.buildWorkerTaskQueue([base], {
			tasks: [{ ...base, name: "PLAN" }], actions: new Map([["PLAN", { status }]]),
		});
		assert.equal(queue.groups.attention.length, 1);
		assert.equal(queue.groups.unavailable.length, 0);
		assert.equal(queue.groups.attention[0].source, "planned");
	}
});
