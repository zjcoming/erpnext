const test = require("node:test");
const assert = require("node:assert/strict");
const { reviewNewAssignmentEntry, openReviewNewAssignment, reviewAssignmentActionState } = require("../../process_simplification/page/production_report_review/production_report_review.js");

test("new review assignments enter the personnel pool without writing a formal assignment", () => {
	const entry = reviewNewAssignmentEntry({ poolEnabled: true, poolAvailable: true });
	assert.equal(entry.label, "安排人员");
	const routes = [];
	let legacyCalls = 0;
	const result = openReviewNewAssignment(entry, { setRoute: (...route) => { routes.push(route); return Promise.resolve(); }, openLegacy: () => legacyCalls++ });
	assert.deepEqual(routes, [["production-workbench", { production_view: "operations" }]]);
	assert.equal(legacyCalls, 0);
	assert.equal(result, undefined, "toolbar callback stays synchronous");
});

test("disabled or unavailable pool preserves the original assignment dialog", () => {
	for (const flags of [{ poolEnabled: false, poolAvailable: true }, { poolEnabled: true, poolAvailable: false }, {}]) {
		const entry = reviewNewAssignmentEntry(flags);
		assert.equal(entry.label, "新增派工");
		let legacyCalls = 0;
		openReviewNewAssignment(entry, { setRoute: () => assert.fail("old mode must not navigate to pool"), openLegacy: () => legacyCalls++ });
		assert.equal(legacyCalls, 1);
	}
});

test("existing assignment controls preserve timer and unassignment restrictions", () => {
	assert.equal(reviewAssignmentActionState({ active_report: "JCWR-1", can_unassign: true }).action, "cancel_session");
	assert.equal(reviewAssignmentActionState({ can_unassign: true }).action, "unassign");
	assert.equal(reviewAssignmentActionState({ can_unassign: false }).action, null);
});
