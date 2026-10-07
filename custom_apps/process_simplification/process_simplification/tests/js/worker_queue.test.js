const test = require("node:test");
const assert = require("node:assert/strict");
const { buildWorkerTaskQueue, workerTaskQueueHtml } = require("../../public/js/worker_reporting.js");
const { workerPlannedCardHtml } = require("../../public/js/worker_prearrangement.js");

test("mixed source readiness orders all ready work before previous, materials and attention", () => {
	const assignments = [
		{ name: "F-MATERIAL", job_card: "JC-1", block_code: "MATERIAL_NOT_TRANSFERRED" },
		{ name: "F-READY", job_card: "JC-2", can_start: true },
		{ name: "F-PREVIOUS", job_card: "JC-3", block_code: "PREVIOUS_OPERATION_PENDING" },
		{ name: "F-REVIEW", job_card: "JC-4", block_code: "PENDING_REPORT" },
	];
	const tasks = [
		{ name: "P-PREVIOUS", job_card: "JC-5", block_code: "PREVIOUS_OPERATION_PENDING" },
		{ name: "P-READY", job_card: "JC-6", can_start: true },
		{ name: "P-MATERIAL", job_card: "JC-7", block_code: "MATERIAL_NOT_FULLY_ISSUED" },
	];
	const queue = buildWorkerTaskQueue(assignments, { tasks });
	assert.equal(queue.total, 7);
	assert.deepEqual(queue.groups.ready.map((entry) => entry.row.name), ["F-READY", "P-READY"]);
	assert.deepEqual(queue.groups.previous.map((entry) => entry.row.name), ["F-PREVIOUS", "P-PREVIOUS"]);
	assert.deepEqual(queue.groups.material.map((entry) => entry.row.name), ["F-MATERIAL", "P-MATERIAL"]);
	const html = workerTaskQueueHtml(queue, { plannedCard: workerPlannedCardHtml });
	assert.ok(html.indexOf('data-plan="P-READY"') < html.indexOf('data-assignment="F-PREVIOUS"'));
	assert.ok(html.indexOf('data-plan="P-PREVIOUS"') < html.indexOf('data-assignment="F-MATERIAL"'));
	assert.ok(html.indexOf('data-plan="P-MATERIAL"') < html.indexOf('data-assignment="F-REVIEW"'));
	assert.match(html, /待主管审核/);
	assert.equal(assignments[1].can_start, true);
});

test("formal activation and active work replace the same planned card without duplicate counts", () => {
	const planned = { name: "PLAN", job_card: "JC", can_start: true };
	const formal = { name: "FORMAL", job_card: "JC", can_start: true };
	let queue = buildWorkerTaskQueue([formal], { tasks: [planned] });
	assert.equal(queue.total, 1);
	assert.equal(queue.groups.ready[0].source, "formal");
	queue = buildWorkerTaskQueue([{ ...formal, active_report: "REPORT" }], {
		tasks: [planned], actions: new Map([["PLAN", { status: "uncertain" }]]),
	});
	assert.equal(queue.total, 0);
	assert.equal(queue.active.length, 1);
});

test("pending activation retains its original check action until an actual active report appears", () => {
	const queue = buildWorkerTaskQueue([{ name: "FORMAL", job_card: "JC", can_start: true }], {
		tasks: [{ name: "PLAN", job_card: "JC", can_start: true }],
		actions: new Map([["PLAN", { status: "uncertain" }]]),
	});
	assert.equal(queue.total, 1);
	assert.equal(queue.groups.ready.length, 0);
	assert.equal(queue.groups.attention[0].source, "planned");
});

test("active work demotes stale ready flags from both sources without changing backend rows", () => {
	const formal = { name: "FORMAL", job_card: "JC-F", can_start: true };
	const planned = { name: "PLAN", job_card: "JC-P", can_start: true };
	const queue = buildWorkerTaskQueue([{ name: "ACTIVE", job_card: "JC-A", active_report: "REPORT" }, formal], { tasks: [planned] });
	assert.equal(queue.groups.ready.length, 0);
	assert.equal(queue.groups.attention.length, 2);
	assert.ok(queue.groups.attention.every(({ row }) => row.block_code === "ACTIVE_WORK_SESSION"));
	assert.equal(formal.can_start, true);
	assert.equal(planned.can_start, true);
});
