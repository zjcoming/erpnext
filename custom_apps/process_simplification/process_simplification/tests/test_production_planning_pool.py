from unittest import TestCase
from unittest.mock import patch
import frappe
from process_simplification.api import production_planning
from process_simplification.production_reporting import dispatch_pool, prearrangement

class TestProductionPlanningPool(TestCase):
    def test_pool_joins_readonly_planning_without_formal_writes(self):
        row=frappe._dict(job_card='JC-1')
        context={'can_plan':True,'can_start':False,'start_block_code':'PREVIOUS_OPERATION_PENDING'}
        with patch.object(dispatch_pool,'get_operation_dispatch_pool',return_value={'rows':[row]}) as read, patch.object(prearrangement,'get_plan_context',return_value={'cards':{'JC-1':context}}) as plans, patch.object(prearrangement,'save_plan') as save:
            result=production_planning.get_planning_pool()
        self.assertEqual(result['rows'][0].planning,context)
        plans.assert_called_once_with(['JC-1']);save.assert_not_called();read.assert_called_once()

    def test_saved_pool_keeps_company_scope_in_count_and_rows_and_can_show_stopped(self):
        with patch.object(dispatch_pool,'_require_access',return_value={'Factory A'}), patch.object(frappe.db,'sql',side_effect=[[(0,)],[]]) as sql, patch.object(frappe,'get_list',return_value=['Factory A']):
            result=production_planning.get_planning_pool(filters={'saved_only':1})
        self.assertEqual(result['pagination']['total_count'],0)
        for call in sql.call_args_list:
            self.assertIn("p.status = 'Planned'",call.args[0]); self.assertIn('p.company in %(companies)s',call.args[0]);self.assertNotIn("wo.status not in",call.args[0]);self.assertEqual(call.args[1]['companies'],('Factory A',))

    def test_saved_pool_rejects_foreign_company_before_query(self):
        with patch.object(dispatch_pool,'_require_access',return_value={'Factory A'}), patch.object(frappe.db,'sql') as sql:
            with self.assertRaises(frappe.PermissionError): production_planning.get_planning_pool(filters={'saved_only':1,'company':'Factory B'})
        self.assertFalse(any("tabJob Card Prearrangement" in str(call.args[0]) for call in sql.call_args_list))

    def test_empty_scope_does_not_become_unrestricted_saved_pool(self):
        with patch.object(dispatch_pool,'_require_access',return_value=set()), patch.object(frappe.db,'sql',side_effect=[[(0,)],[]]) as sql, patch.object(frappe,'get_list',return_value=[]):
            production_planning.get_planning_pool(filters={'saved_only':1})
        for call in sql.call_args_list:self.assertIn('1 = 0',call.args[0])
