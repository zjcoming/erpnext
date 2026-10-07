const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const shared = require("../../public/js/operation_dispatch_pool.js");
const context = { ...shared, module: { exports: {} }, setTimeout, clearTimeout };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../../public/js/production_prearrangement.js"), "utf8"), context);
const ui = context.module.exports;
const row = (name, override = {}) => ({ job_card: name, work_order: "WO", operation_id: name, company: "C", operation: "切割", for_quantity: 10,
 planning: { can_plan: true, can_start: false, start_block_code: "MATERIAL_NOT_FULLY_ISSUED" }, ...override });
const page = (rows) => ({ rows, companies: ["C"], pagination: { page: 1, total_count: rows.length, total_pages: 1 } });
const allocation = (names) => Object.fromEntries(names.map(name => [name, [{ employee: "EMP", assigned_qty: 10 }]]));
const setup = (options = {}) => ui.createProductionPlanController({ read: async () => page([row("A"), row("B")]),
 save: async ({job_card}) => ({name: "P"+job_card, job_card, status: "Planned", revision: 1}), recheck: async () => ({cards: {}}), ...options });
const pending = () => { let resolve; const promise = new Promise(r => resolve = r); return {promise,resolve}; };

test("waiting material and previous operation are selectable plans, never declared startable", () => {
 for (const block of ["MATERIAL_NOT_FULLY_ISSUED", "PREVIOUS_OPERATION_PENDING"]) {
  const task = row("A", {planning: {can_plan: true, can_start: false, start_block_code: block, plan: {status: "Planned"}}});
  assert.equal(ui.productionPlanSelectable(task), true);
  assert.doesNotMatch(ui.productionPlanState(task).label, /可开工/);
 }
 assert.equal(ui.productionPlanSelectable(row("A", {planning: {can_plan: false}})), false);
});
test("single default receives full amount; a team requires explicit quantity allocation", () => {
 const task = row("A", {planning: {default_roster: {workers: [{employee: "E1"}]}}});
 assert.equal(ui.productionPlanInitialAllocations(task)[0].assigned_qty, 10);
 task.planning.default_roster.workers.push({employee: "E2"});
 assert.ok(ui.productionPlanInitialAllocations(task).every(e => e.assigned_qty === 0));
 const existing = [{employee:"E3", assigned_qty: 10}];
 task.planning.plan = {status: "Planned", allocations: existing};
 assert.equal(ui.productionPlanInitialAllocations(task)[0].employee, "E3");
});
test("preview validation rejects duplicated worker or overallocated production", () => {
 assert.match(ui.productionPlanValidate([row("A")], {A:[{employee:"E",assigned_qty:10},{employee:"E",assigned_qty:10}]}), /重复/);
 assert.match(ui.productionPlanValidate([row("A")], {A:[{employee:"E",assigned_qty:20}]}), /合计/);
});
test("save includes the reviewed revision and frozen task identity", async () => {
 const writes = [];
 const c = setup({ read: async () => page([row("A", {planning:{can_plan:true,plan:{revision:4,supervisor:"boss"}}})]),
  save: async args => {writes.push(args);return {name:"P",job_card:"A",status:"Planned"};} });
 await c.load();c.selectPage(true);await c.execute(allocation(["A"]));
 assert.equal(writes.length, 1);assert.equal(writes[0].expected_revision, 4);
 assert.equal(writes[0].expected_operation_id, "A");assert.equal(writes[0].supervisor, "boss");
});
test("read does not call save and clears stale selection on refresh", async () => {
 let writes=0;const c=setup({save:async()=>{writes++;}});
 await c.load();c.selectPage(true);await c.load();
 assert.equal(writes,0);assert.equal(c.state.selected.size,0);
});
test("selection never crosses companies", async () => {
 const c=setup({read:async()=>page([row("A"),row("B",{company:"OTHER"})])});
 await c.load();c.select("A",true);assert.throws(()=>c.select("B",true),/同一公司/);
});
test("stop or leaving view finishes current save and skips subsequent tasks", async () => {
 const p=pending(),calls=[];const c=setup({save:async args=>{calls.push(args.job_card);await p.promise;return{name:"P",job_card:args.job_card,status:"Planned"};}});
 await c.load();c.selectPage(true);const run=c.execute(allocation(["A","B"]));c.deactivate();p.resolve();await run;
 assert.deepEqual(calls,["A"]);assert.equal(c.state.results[1].status,"skipped");
});
test("uncertain save stops remaining batch and blocks re-selection after refresh", async () => {
 let writes=0;const c=setup({save:async()=>{writes++;throw{status:0};}});
 await c.load();c.selectPage(true);await c.execute(allocation(["A","B"]));
 assert.equal(writes,1);assert.equal(c.state.results[0].status,"uncertain");assert.equal(c.state.results[1].status,"skipped");
 assert.equal(ui.productionPlanSelectable(c.state.rows[0]),false);
});
test("timeout readback reports existing server revision without claiming request ownership", async () => {
 const c=setup({save:async()=>{throw{status:0};},recheck:async()=>({cards:{A:{plan:{revision:1,status:"Planned"}}}})});
 await c.load();c.select("A",true);await c.execute(allocation(["A"]));
 assert.equal(c.state.results[0].status,"verified");assert.match(c.state.results[0].message,/核对/);
});
test("business error doesn't erase successful rows and doesn't stop independent plans", async () => {
 const c=setup({save:async args=>{if(args.job_card==="A")throw{status:417,message:"任务变化"};return{name:"P",job_card:args.job_card,status:"Planned"};}});
 await c.load();c.selectPage(true);await c.execute(allocation(["A","B"]));
 assert.equal(c.state.results[0].status,"failed");assert.equal(c.state.results[1].status,"saved");
});
test("late read result cannot replace newer filter results", async () => {
 const p=pending();let reads=0;const c=setup({read:async()=> ++reads===1?p.promise:page([row("NEW")])});
 const old=c.load();await c.load();p.resolve(page([row("OLD")]));await old;assert.equal(c.state.rows[0].job_card,"NEW");
});
test("HTML escapes product and worker content",()=>{
 const task=row("A",{item_name:'<img src=x onerror="boom">',planning:{can_plan:true,plan:{status:"Planned",allocations:[{employee:'<script>',assigned_qty:10}]}}});
 const html=ui.productionPlanRowHtml(task,false,false);assert.doesNotMatch(html,/<img|<script>/);assert.match(html,/&lt;script&gt;/);
});

test("page status counts separate unarranged, normal waiting and tasks needing intervention", () => {
 const rows = [
  row("NEW"),
  row("WAIT", {planning:{can_plan:true,can_start:false,start_block_code:"PREVIOUS_OPERATION_PENDING",plan:{status:"Planned"}}}),
  row("INVALID", {planning:{can_plan:true,can_start:false,start_block_code:"PLAN_NEEDS_REVIEW",plan:{status:"Planned"}}}),
  row("ACTIVE", {has_assignment_history:true,planning:{can_plan:false}}),
  row("STOPPED", {planning:{can_plan:false,plan:{status:"Planned"}}}),
 ];
 const counts = ui.productionPlanPageCounts(rows);
 assert.equal(counts.all,5); assert.equal(counts.unscheduled,1); assert.equal(counts.scheduled,2); assert.equal(counts.attention,2);
 assert.equal(ui.productionPlanCategory(rows[1]),"scheduled");
 assert.equal(ui.productionPlanCategory(rows[2]),"attention");
});

test("changing the page status clears selection and bulk selection excludes hidden tasks", async () => {
 let reads = 0;
 const c = setup({read:async()=>{reads++;return page([
  row("NEW"), row("WAIT",{planning:{can_plan:true,plan:{status:"Planned"}}}), row("OTHER",{company:"OTHER"})
 ]);}});
 await c.load(); c.select("WAIT",true); c.setStatusFilter("unscheduled");
 assert.equal(c.state.selected.size,0);
 c.selectPage(true);
 assert.deepEqual(Array.from(c.state.selected),["NEW"]);
 c.select("WAIT",true);
 assert.deepEqual(Array.from(c.state.selected),["NEW"],"hidden tasks cannot be added to a batch");
 assert.equal(reads,1,"status tabs must not query or imply a global count");
});

test("explicit order entry clears stale company, local status, operation and saved-only filters", async () => {
 const c=setup();await c.load();c.state.filters={company:"Factory",operation:"OLD",search:"PREVIOUS",saved_only:1};
 c.setStatusFilter("attention");
 const request=ui.productionPlanActivationRequest(c.state,{search:" SAL-ORD-00001 "});
 assert.equal(request.page,1);assert.equal(request.statusFilter,"all");
	assert.equal(request.filters.company,"");assert.equal(request.filters.operation,"");
 assert.equal(request.filters.saved_only,0);assert.equal(request.filters.search,"SAL-ORD-00001");
 assert.equal(ui.productionPlanActivationRequest(c.state,{search:"SAL-ORD-00001",company:" New Factory "}).filters.company,"New Factory");
 await c.load(request);assert.equal(c.state.statusFilter,"all");assert.equal(c.state.selected.size,0);
});

test("ordinary re-entry does not clear an existing search or page-status choice",()=>{
 const state={filters:{company:"Factory",search:"WO-8",operation:"打磨",saved_only:1},statusFilter:"scheduled"};
 assert.equal(ui.productionPlanActivationRequest(state),null);
 assert.equal(ui.productionPlanActivationRequest(state,{}),null);
 assert.equal(state.filters.search,"WO-8");assert.equal(state.filters.company,"Factory");assert.equal(state.statusFilter,"scheduled");
 assert.equal(ui.productionPlanActivationRequest(state,{search:""}).filters.search,"");
});

test("saved plans expose a direct edit action and invalid unstarted plans remain cancellable",()=>{
 const planned={can_plan:true,plan:{status:"Planned",allocations:[{employee_name:"张师傅",assigned_qty:10}]}};
 const editable=ui.productionPlanRowHtml(row("A",{item_name:"支架",planning:planned}),true,false);
 assert.match(editable,/data-plan-open="A"[^>]*>修改安排/);
 assert.match(editable,/data-plan-cancel="A"[^>]*>撤销安排/);
 assert.match(editable,/production-personnel-source[^>]*><summary>任务来源/);
 assert.ok(editable.indexOf("支架") < editable.indexOf("任务来源"));
 const stopped=ui.productionPlanRowHtml(row("A",{planning:{...planned,can_plan:false,block_message:"工单已停工"}}),false,false);
 assert.doesNotMatch(stopped,/data-plan-open=/);
 assert.match(stopped,/data-plan-cancel="A"/);assert.match(stopped,/工单已停工/);
 const active=ui.productionPlanRowHtml(row("A",{has_assignment_history:true,planning:{can_plan:false,plan:{status:"Activated"}}}),false,false);
 assert.doesNotMatch(active,/data-plan-open=|data-plan-cancel=/);
});

test("mounted order entry resets stale filters while ordinary re-entry retains the selected view", async () => {
 const nodes=new Map(),calls=[];
 function node(selector) {
  if(nodes.has(selector))return nodes.get(selector);
  const value={value:"",properties:{},
   addClass(){return this;},html(text){this.content=text;return this;},text(text){this.content=text;return this;},
   val(value){if(arguments.length){this.value=value;return this;}return this.value;},
   prop(name,value){this.properties[name]=value;return this;},on(){return this;},find:node,
  };nodes.set(selector,value);return value;
 }
 const scope={...shared,module:{exports:{}},setTimeout,clearTimeout,$:(value)=>value,
  frappe:{router:{on(){}},get_route:()=>["production-workbench"],call:async(options)=>{calls.push(options);return{message:page([row("A")])};}}
 };
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../../public/js/production_prearrangement.js"),"utf8"),scope);
 const mounted=scope.module.exports.mountProductionPrearrangement({host:node("host"),openAssignment(){}});
 mounted.controller.state.filters={company:"Factory",operation:"打磨",search:"OLD",saved_only:1};
 mounted.controller.state.statusFilter="attention";
 await mounted.activate({search:"SAL-ORD-001"});
 assert.equal(calls[0].args.filters.search,"SAL-ORD-001");assert.equal(calls[0].args.filters.company,"");
 assert.equal(calls[0].args.filters.operation,"");assert.equal(calls[0].args.filters.saved_only,0);
 assert.equal(mounted.controller.state.statusFilter,"all");
 mounted.controller.state.filters.company="Current Factory";
 mounted.controller.setStatusFilter("unscheduled");await mounted.activate({});
 assert.equal(calls[1].args.filters.search,"SAL-ORD-001");assert.equal(calls[1].args.filters.company,"Current Factory");assert.equal(mounted.controller.state.statusFilter,"unscheduled");
 assert.match(nodes.get(".production-personnel-page-states").content,/本页状态/);
});
