const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../../process_simplification/page/process_access_management/process_access_management.js"), "utf8");

function deferred() {
	let resolve, reject;
	const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
	return { promise, resolve, reject };
}

function response(user, companies = []) {
	return { message: { user: { name: user }, companies, scope_options: { companies: ["Factory A"] } } };
}

function setup() {
	const state = { calls: [], selectedUser: "user-a@example.test", renderCount: 0, companyChecked: false, contentHidden: true };
	const elements = new Map();
	const root = { find(selector) {
		if (!elements.has(selector)) elements.set(selector, {
			addClass() { if (selector === "[data-content]") state.contentHidden = true; return this; },
			removeClass() { if (selector === "[data-content]") state.contentHidden = false; return this; },
			text(value) { state.placeholder = value; return this; },
			html(value) { state.companyChecked = /\schecked(?:\s|>)/.test(value); return this; },
			val() { return ""; },
		});
		return elements.get(selector);
	} };
	const frappe = {
		pages: { "process-access-management": {} },
		utils: { escape_html: String }, show_alert() {},
		call(args) {
			const pending = deferred();
			state.calls.push({ ...args, ...pending });
			return pending.promise;
		},
	};
	const context = vm.createContext({ frappe, __: String });
	vm.runInContext(`${source}\nglobalThis.AccessManagement = ProcessSimplificationAccessManagement;`, context);
	const page = Object.create(context.AccessManagement.prototype);
	page.page = {
		add_field(df) { state.userChanged = df.change; return { get_value: () => state.selectedUser }; },
		btn_primary: { prop(_, value) { state.saveDisabled = value; } },
	};
	page.root = root;
	page.make_user_field();
	// Exercise the real company renderer so an unexpected reload loses the draft checkbox.
	page.render = function () {
		state.renderCount++;
		this.render_companies();
		state.contentHidden = false;
		this.page.btn_primary.prop("disabled", false);
	};
	page.checked_values = (attribute) => attribute === "company-scope" && state.companyChecked ? ["Factory A"] : [];
	return { page, state };
}

async function selectAndLoad(state, user = state.selectedUser, companies = []) {
	state.selectedUser = user;
	const loading = state.userChanged();
	state.calls.at(-1).resolve(response(user, companies));
	await loading;
}

test("autocomplete followed by blur preserves the first company click without reloading", async () => {
	const { page, state } = setup();
	await selectAndLoad(state);
	const blur = state.userChanged();
	state.companyChecked = true;
	// Resolve every request: a duplicate read would redraw the saved unchecked value.
	state.calls.forEach((call) => call.resolve(response(state.selectedUser)));
	await blur;
	assert.equal(state.companyChecked, true);
	assert.equal(state.calls.length, 1);
	assert.equal(state.renderCount, 1);
	assert.equal(page.current.user.name, state.selectedUser);
	assert.equal(state.saveDisabled, false);
});

test("repeated Link change while the same user is loading issues one read", async () => {
	const { state } = setup();
	const first = state.userChanged();
	const repeated = state.userChanged();
	state.calls.forEach((call) => call.resolve(response(state.selectedUser)));
	await Promise.all([first, repeated]);
	assert.equal(state.calls.length, 1);
	assert.equal(state.renderCount, 1);
});

test("switching users hides the old panel and ignores out-of-order responses", async () => {
	const { page, state } = setup();
	await selectAndLoad(state);
	state.selectedUser = "user-b@example.test";
	const loadingB = state.userChanged();
	assert.equal(state.contentHidden, true);
	assert.equal(state.saveDisabled, true);
	state.selectedUser = "user-c@example.test";
	const loadingC = state.userChanged();
	state.calls[2].resolve(response("user-c@example.test", ["Factory A"]));
	await loadingC;
	state.calls[1].resolve(response("user-b@example.test"));
	await loadingB;
	assert.equal(page.current.user.name, "user-c@example.test");
	assert.equal(state.companyChecked, true);
	assert.equal(state.renderCount, 2);
});

test("clearing the user invalidates the pending response and leaves saving disabled", async () => {
	const { page, state } = setup();
	const loading = state.userChanged();
	state.selectedUser = "";
	await state.userChanged();
	state.calls[0].resolve(response("user-a@example.test"));
	await loading;
	assert.equal(page.current, null);
	assert.equal(state.renderCount, 0);
	assert.equal(state.contentHidden, true);
	assert.equal(state.saveDisabled, true);
	assert.equal(state.placeholder, "请先选择一个系统用户");
});

test("A to B to A accepts only the newest A response", async () => {
	const { page, state } = setup();
	const oldA = state.userChanged();
	state.selectedUser = "user-b@example.test";
	const loadingB = state.userChanged();
	state.selectedUser = "user-a@example.test";
	const newA = state.userChanged();
	state.calls[0].resolve(response("user-a@example.test"));
	await oldA;
	assert.equal(state.renderCount, 0);
	const repeated = state.userChanged();
	state.calls[2].resolve(response("user-a@example.test", ["Factory A"]));
	state.calls[1].resolve(response("user-b@example.test"));
	await Promise.all([newA, loadingB, repeated]);
	assert.equal(state.calls.length, 3);
	assert.equal(page.current.user.name, "user-a@example.test");
	assert.equal(state.renderCount, 1);
	assert.equal(state.companyChecked, true);
});

test("a failed read can be retried for the same user", async () => {
	const { page, state } = setup();
	const failed = state.userChanged();
	state.calls[0].reject(new Error("temporary read failure"));
	await assert.rejects(failed, /temporary read failure/);
	assert.equal(state.saveDisabled, true);
	await selectAndLoad(state);
	assert.equal(state.calls.length, 2);
	assert.equal(page.current.user.name, state.selectedUser);
});

test("saving submits the first company selection and explicitly reloads the saved user", async () => {
	const { page, state } = setup();
	await selectAndLoad(state);
	state.companyChecked = true;
	const saving = page.save();
	assert.match(state.calls[1].method, /\.set_user_access$/);
	assert.equal(state.calls[1].type, "POST");
	assert.equal(state.calls[1].args.user, state.selectedUser);
	assert.deepEqual(state.calls[1].args.companies, ["Factory A"]);
	state.calls[1].resolve({ message: true });
	await new Promise(setImmediate);
	assert.match(state.calls[2].method, /\.get_user_access$/);
	state.calls[2].resolve(response(state.selectedUser, ["Factory A"]));
	await saving;
	assert.equal(state.companyChecked, true);
	assert.equal(state.renderCount, 2);
	assert.equal(state.saveDisabled, false);
});

test("saving never applies an old panel when the user field has changed", async () => {
	const { page, state } = setup();
	await selectAndLoad(state);
	state.selectedUser = "user-b@example.test";
	await page.save();
	assert.equal(state.calls.length, 1);
});

test("a completed save for A does not reload B and discard B's unsaved selection", async () => {
	const { page, state } = setup();
	await selectAndLoad(state);
	const savingA = page.save();
	await selectAndLoad(state, "user-b@example.test");
	state.companyChecked = true;
	state.calls[1].resolve({ message: true });
	await savingA;
	assert.equal(state.calls.length, 3);
	assert.equal(page.current.user.name, "user-b@example.test");
	assert.equal(state.companyChecked, true);
	assert.equal(state.renderCount, 2);
});
