"""Regression tests for the T/T+1 paper plan and no-fabricated-fill policy."""
import unittest
from src.dual_plan import build_plan


class DualPlanTests(unittest.TestCase):
    def report(self):
        return dict(date="2026-10-12", status="scanned",
                    opening_candidates=[dict(code="000001.XSHE", name="测试", component="FB_dip"),
                                        dict(code="000002.XSHE", name="测试2", component="LB")])

    def test_separate_entry_and_exit_dates(self):
        plan = build_plan(self.report(), "2026-10-13", "2026-10-14")
        self.assertEqual(plan["A"]["orders"][0]["buy_date"], "2026-10-12")
        self.assertEqual(plan["A"]["orders"][0]["sell_date"], "2026-10-13")
        self.assertEqual(plan["B"]["orders"][0]["buy_date"], "2026-10-13")
        self.assertEqual(plan["B"]["orders"][0]["sell_date"], "2026-10-14")
        self.assertEqual(plan["initial_cash_per_account"], 200000.)
        self.assertTrue(all(not o["execution_confirmed"] and o["shares"] == 0
                            for group in ("A", "B") for o in plan[group]["orders"]))

    def test_reject_incomplete_scan(self):
        report = self.report()
        report["status"] = "failed_data_incomplete"
        with self.assertRaises(ValueError):
            build_plan(report, "2026-10-13", "2026-10-14")

    def test_reject_invalid_date_order(self):
        with self.assertRaises(ValueError):
            build_plan(self.report(), "2026-10-12", "2026-10-14")


if __name__ == "__main__":
    unittest.main()
