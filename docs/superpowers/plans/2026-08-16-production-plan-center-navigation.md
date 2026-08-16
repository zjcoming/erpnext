# Production Plan Center Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the custom production workbench to “生产计划中心” everywhere users see it and render a valid `factory` icon without changing the `production-workbench` route.

**Architecture:** Keep the existing route and APIs stable. Update fixtures and page metadata for new installs, update the existing navigation repair routine as the single source of navigation values, and register a new wrapper patch so installed sites rerun that idempotent repair.

**Tech Stack:** Frappe v16 Workspace fixtures, Python patches and integration tests, JavaScript, Node test runner, Bench test runner.

## Global Constraints

- The visible name is exactly `生产计划中心`.
- The Workspace Sidebar icon is exactly `factory`.
- The route remains exactly `production-workbench`.
- The internal action/API name `create_work_order` remains unchanged.
- This change creates or migrates no business documents.

---

### Task 1: Idempotent Navigation Migration

**Files:**
- Create: `custom_apps/process_simplification/process_simplification/patches/v0_0/rename_production_workbench_to_plan_center.py`
- Modify: `custom_apps/process_simplification/process_simplification/patches/v0_0/add_production_workbench_navigation.py`
- Modify: `custom_apps/process_simplification/process_simplification/patches.txt`
- Test: `custom_apps/process_simplification/process_simplification/tests/test_desktop_navigation_integration.py`

**Interfaces:**
- Consumes: `add_production_workbench_navigation.execute() -> None`.
- Produces: patch `process_simplification.patches.v0_0.rename_production_workbench_to_plan_center`.

- [ ] **Step 1: Write the failing integration test**

Seed the old label/icon, invoke the new patch, and assert the repaired records:

```python
from process_simplification.patches.v0_0.rename_production_workbench_to_plan_center import execute

production_sidebar.label = "生产工作台"
production_sidebar.icon = "manufacturing"
sidebar.save(ignore_permissions=True)
execute()

sidebar.reload()
workspace.reload()
production_sidebar = next(item for item in sidebar.items if item.link_to == "production-workbench")
production_workspace = next(item for item in workspace.links if item.link_to == "production-workbench")
self.assertEqual(frappe.db.get_value("Page", "production-workbench", "title"), "生产计划中心")
self.assertEqual(production_sidebar.label, "生产计划中心")
self.assertEqual(production_sidebar.icon, "factory")
self.assertEqual(production_workspace.label, "生产计划中心")
```

- [ ] **Step 2: Run the focused integration test and verify RED**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe bench \
  --site development.localhost run-tests --app process_simplification \
  --module process_simplification.tests.test_desktop_navigation_integration
```

Expected: FAIL because the new patch is absent or the old values remain.

- [ ] **Step 3: Implement the migration**

Use exact constants in the existing repair routine:

```python
PRODUCTION_CENTER_LABEL = "生产计划中心"
PRODUCTION_CENTER_ICON = "factory"
```

Add the wrapper patch:

```python
from process_simplification.patches.v0_0.add_production_workbench_navigation import execute

__all__ = ["execute"]
```

Register after the old patch in `[post_model_sync]`:

```text
process_simplification.patches.v0_0.rename_production_workbench_to_plan_center
```

- [ ] **Step 4: Run the focused integration test and verify GREEN**

Run the Step 2 command again. Expected: all desktop-navigation integration tests PASS, including missing-item repair and ordering before `shortage-purchase-planning`.

- [ ] **Step 5: Commit the migration**

```bash
git add custom_apps/process_simplification/process_simplification/patches.txt \
  custom_apps/process_simplification/process_simplification/patches/v0_0/add_production_workbench_navigation.py \
  custom_apps/process_simplification/process_simplification/patches/v0_0/rename_production_workbench_to_plan_center.py \
  custom_apps/process_simplification/process_simplification/tests/test_desktop_navigation_integration.py
git commit -m "fix(process-simplification): rename production plan center navigation"
```

### Task 2: Exported Fixtures and Page Title

**Files:**
- Modify: `custom_apps/process_simplification/process_simplification/workspace_sidebar/process_simplification.json`
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/workspace/process_simplification/process_simplification.json`
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.json`
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.py`
- Modify: `custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench/production_workbench.js`
- Test: `custom_apps/process_simplification/process_simplification/tests/js/workspace_sidebar.test.js`

**Interfaces:**
- Consumes: route key `production-workbench` and the approved label/icon.
- Produces: fixture defaults and runtime page header “生产计划中心”.

- [ ] **Step 1: Write the failing source contract test**

```javascript
test("production plan center keeps its route and approved navigation identity", () => {
    const pageSource = fs.readFileSync(
        path.join(pageDirectory, "production_workbench", "production_workbench.js"),
        "utf8"
    );
    const sidebar = JSON.parse(fs.readFileSync(sidebarFixturePath, "utf8"));
    const production = sidebar.items.find((item) => item.link_to === "production-workbench");

    assert.equal(production.label, "生产计划中心");
    assert.equal(production.icon, "factory");
    assert.match(pageSource, /title: __\("生产计划中心"\)/);
});
```

- [ ] **Step 2: Run the focused Node test and verify RED**

```bash
node --test custom_apps/process_simplification/process_simplification/tests/js/workspace_sidebar.test.js
```

Expected: FAIL because the fixture and JavaScript title still use the old identity.

- [ ] **Step 3: Update fixtures and page metadata**

Retain the route and use these values:

```json
{
  "icon": "factory",
  "label": "生产计划中心",
  "link_to": "production-workbench"
}
```

Set the Workspace link, Page JSON title, Python context title, and JavaScript page title to `生产计划中心`.

- [ ] **Step 4: Run the focused Node test and verify GREEN**

Run the Step 2 command again. Expected: all workspace-sidebar JavaScript tests PASS.

- [ ] **Step 5: Commit the fixture and page changes**

```bash
git add custom_apps/process_simplification/process_simplification/workspace_sidebar/process_simplification.json \
  custom_apps/process_simplification/process_simplification/process_simplification/workspace/process_simplification/process_simplification.json \
  custom_apps/process_simplification/process_simplification/process_simplification/page/production_workbench \
  custom_apps/process_simplification/process_simplification/tests/js/workspace_sidebar.test.js
git commit -m "feat(process-simplification): present production plan center"
```

### Task 3: Site Migration and Full Verification

**Files:**
- Verify only; no production source files should change.

**Interfaces:**
- Consumes: Tasks 1 and 2 plus the registered patch.
- Produces: updated development navigation, compiled assets, and regression evidence.

- [ ] **Step 1: Migrate the development site**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost migrate
```

Expected: migration succeeds and applies the new navigation patch.

- [ ] **Step 2: Verify live navigation records**

Use Bench execute to read Page `production-workbench` and Workspace Sidebar `process-simplification`. Confirm label/title `生产计划中心`, icon `factory`, and route `production-workbench`.

- [ ] **Step 3: Run all backend tests**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench --site development.localhost run-tests --app process_simplification
```

Expected: all unit and integration tests PASS.

- [ ] **Step 4: Run all frontend tests**

```bash
node --test custom_apps/process_simplification/process_simplification/tests/js/*.test.js
```

Expected: all JavaScript tests PASS.

- [ ] **Step 5: Build assets and verify the diff**

```bash
docker compose exec -T -w /workspace/erpnext/development/frappe-bench frappe \
  bench build --app process_simplification
git diff --check
git status --short
```

Expected: build exits zero, `git diff --check` emits no errors, and only intended plan-tracking changes remain.
