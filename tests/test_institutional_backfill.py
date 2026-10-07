import tempfile
import unittest
from datetime import date

from market_data.database import MarketDatabase
from market_data.disclosures import institutional_activity
from market_data.institutional_backfill import (
    InstitutionalActivity,
    InstitutionalBackfill,
    month_targets,
    parse_cdsl_activity,
)


PRE_ROUTE = """
<table>
<tr><th>Reporting Date</th><th>Debt/Equity</th><th>Gross Purchases(Rs Crore)</th>
<th>Gross Sales(Rs Crore)</th><th>Net Investment (Rs Crore)</th></tr>
<tr><td rowspan="2">01-JAN-1999</td><td>Equity</td><td>32.50</td><td>7.50</td><td>25.00</td></tr>
<tr><td>Debt</td><td>1</td><td>2</td><td>-1</td></tr>
</table>
"""

ROUTED = """
<table>
<tr><th>Reporting Date</th><th>Debt/Equity</th><th>Investment Route</th>
<th>Gross Purchases (Rs Crore)</th><th>Gross Sales (Rs Crore)</th>
<th>Net Investment (Rs Crore)</th></tr>
<tr><td rowspan="6">02-01-2024</td><td rowspan="3">Equity</td>
<td>Stock Exchange</td><td>10</td><td>4</td><td>6</td></tr>
<tr><td>Primary market &amp; others</td><td>2</td><td>1</td><td>1</td></tr>
<tr><td>Sub-total</td><td>12</td><td>5</td><td>7</td></tr>
<tr><td rowspan="3">Debt</td><td>Stock Exchange</td><td>3</td><td>1</td><td>2</td></tr>
<tr><td>Primary market &amp; others</td><td>0</td><td>0</td><td>0</td></tr>
<tr><td>Sub-total</td><td>3</td><td>1</td><td>2</td></tr>
</table>
"""


class FakeClient:
    def fetch_month(self, target):
        return [
            InstitutionalActivity(
                date(target.year, target.month, 1),
                12.0,
                5.0,
                7.0,
                {"target": target.isoformat()},
            )
        ]


class InstitutionalBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = MarketDatabase(
            "sqlite:///{}".format(self.temp.name + "/market.db")
        )
        self.database.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_parses_pre_route_and_routed_equity_totals(self):
        old = parse_cdsl_activity(PRE_ROUTE)
        new = parse_cdsl_activity(ROUTED)
        self.assertEqual((old[0].buy_value, old[0].net_value), (32.5, 25.0))
        self.assertEqual((new[0].buy_value, new[0].net_value), (12.0, 7.0))

    def test_month_targets_use_partial_final_month(self):
        self.assertEqual(
            list(month_targets(date(2024, 1, 15), date(2024, 3, 4))),
            [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 4)],
        )

    def test_backfill_is_resumable_and_upserts_cdsl_rows(self):
        service = InstitutionalBackfill(self.database, FakeClient())
        targets = service.plan(date(2024, 1, 1), date(2024, 2, 29))
        summary = service.run(targets)
        self.assertEqual(summary, {
            "attempted": 2, "completed": 2, "failed": 0, "rows": 2,
        })
        self.assertEqual(
            service.plan(date(2024, 1, 1), date(2024, 2, 29)), []
        )
        with self.database.engine.connect() as connection:
            rows = connection.execute(institutional_activity.select()).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].source, "CDSL")


if __name__ == "__main__":
    unittest.main()
