"""Historical official CDSL FII/FPI activity backfill."""

import calendar
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Iterable, List, Mapping, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from market_data.database import MarketDatabase
from market_data.disclosures import institutional_activity


CDSL_ARCHIVE_URL = "https://www.cdslindia.com/Publications/FIITrends.aspx"
CDSL_ARCHIVE_DATA_URL = "https://www.cdslindia.com/Publications/"
DATASET = "institutional_activity"


@dataclass(frozen=True)
class InstitutionalActivity:
    trade_date: date
    buy_value: float
    sell_value: float
    net_value: float
    raw: Mapping[str, object]


def month_targets(start: date, end: date) -> Iterable[date]:
    current = date(start.year, start.month, 1)
    while current <= end:
        last = date(
            current.year,
            current.month,
            calendar.monthrange(current.year, current.month)[1],
        )
        yield min(last, end)
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)


def _number(value: str) -> float:
    return float(value.replace(",", "").strip())


def parse_cdsl_activity(html: str) -> List[InstitutionalActivity]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if table is None:
        raise ValueError("CDSL archive response contained no activity table")
    rows = [
        [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
        for row in table.find_all("tr")
    ]
    if not rows or "Reporting Date" not in rows[0]:
        raise ValueError("CDSL archive response has an unknown table format")
    routed = "Investment Route" in rows[0]
    results = []
    current_date = None
    current_asset = None
    for cells in rows[1:]:
        if not cells:
            continue
        parsed_date = None
        for date_format in ("%d-%b-%Y", "%d-%m-%Y"):
            try:
                parsed_date = datetime.strptime(cells[0], date_format).date()
                break
            except ValueError:
                pass
        if parsed_date is not None:
            current_date = parsed_date
            current_asset = cells[1] if len(cells) > 1 else None
            if not routed and current_asset == "Equity":
                values = cells[2:5]
            else:
                continue
        elif routed:
            if cells[0] in {"Equity", "Debt", "Debt-VRR", "Debt FAR", "Hybrid", "MF", "AIF"}:
                current_asset = cells[0]
                continue
            if current_asset != "Equity" or cells[0] != "Sub-total":
                continue
            values = cells[1:4]
        else:
            continue
        if current_date is None or len(values) != 3:
            continue
        buy, sell, net = map(_number, values)
        reconciliation_delta = round(net - (buy - sell), 6)
        if abs(reconciliation_delta) > 0.15:
            raise ValueError(
                "CDSL FII/FPI values do not reconcile for {}".format(current_date)
            )
        results.append(
            InstitutionalActivity(
                current_date,
                buy,
                sell,
                net,
                {
                    "reporting_date": current_date.isoformat(),
                    "asset_class": "Equity",
                    "scope": "all investment routes" if routed else "published equity total",
                    "buy_value": buy,
                    "sell_value": sell,
                    "net_value": net,
                    "calculated_net_value": round(buy - sell, 6),
                    "reconciliation_delta": reconciliation_delta,
                    "published_net_preserved": True,
                },
            )
        )
    if not results:
        raise ValueError("CDSL archive response contained no daily equity activity")
    return results


class CDSLInstitutionalClient:
    def __init__(
        self,
        timeout: float = 45,
        delay: float = 1.0,
        session: Optional[requests.Session] = None,
        sleep=time.sleep,
    ):
        self.timeout = timeout
        self.delay = delay
        self.session = session or requests.Session()
        self.sleep = sleep
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
            ),
            "Referer": CDSL_ARCHIVE_URL,
        }

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        if self.delay:
            self.sleep(self.delay)
        response = self.session.request(
            method, url, headers=self.headers, timeout=self.timeout, **kwargs
        )
        response.raise_for_status()
        if not response.content:
            raise ValueError("CDSL returned an empty archive response")
        return response

    @staticmethod
    def _hidden(html: str, name: str) -> str:
        match = re.search(
            r'name="{}"[^>]*value="([^"]*)"'.format(re.escape(name)), html
        )
        if not match:
            raise ValueError("CDSL archive form omitted {}".format(name))
        return match.group(1)

    def fetch_month(self, target: date) -> List[InstitutionalActivity]:
        landing = self._request("GET", CDSL_ARCHIVE_URL)
        form = {
            name: self._hidden(landing.text, name)
            for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")
        }
        form.update(
            {
                "grpArchive": "rdbAfter",
                "selectedDate": "{} {}, {}".format(
                    target.strftime("%B"), target.day, target.year
                ),
                "ctl30": "Go",
            }
        )
        selection = self._request("POST", CDSL_ARCHIVE_URL, data=form)
        match = re.search(r"window\.open\('([^']+)'", selection.text)
        if not match:
            raise ValueError("CDSL archive did not provide a result page")
        report = self._request(
            "GET", urljoin(CDSL_ARCHIVE_DATA_URL, match.group(1))
        )
        return parse_cdsl_activity(report.text)

    def close(self) -> None:
        self.session.close()


class InstitutionalBackfill:
    def __init__(self, database: MarketDatabase, client: CDSLInstitutionalClient):
        self.database = database
        self.client = client

    def plan(self, start: date, end: date, retry_failed: bool = False) -> List[date]:
        statuses = ("failed",) if retry_failed else ("success",)
        recorded = self.database.checkpoint_dates(
            "cdsl", DATASET, start, end, statuses
        )
        targets = list(month_targets(start, end))
        return [
            target for target in targets
            if (target in recorded) == retry_failed
        ]

    def run(self, targets: Iterable[date]) -> Dict[str, int]:
        targets = list(targets)
        run_id = self.database.start_run("cdsl", DATASET)
        summary = {"attempted": 0, "completed": 0, "failed": 0, "rows": 0}
        try:
            for target in targets:
                summary["attempted"] += 1
                key = target.strftime("%Y-%m")
                try:
                    rows = self.client.fetch_month(target)
                    written = self._upsert(rows)
                    self.database.record_checkpoint(
                        "cdsl", DATASET, key, "success",
                        checkpoint_date=target, row_count=written,
                    )
                    summary["completed"] += 1
                    summary["rows"] += written
                except Exception as error:
                    self.database.record_checkpoint(
                        "cdsl", DATASET, key, "failed",
                        checkpoint_date=target, error=str(error),
                    )
                    self.database.record_error(
                        run_id, "cdsl", DATASET, key, error
                    )
                    summary["failed"] += 1
        finally:
            self.database.finish_run(
                run_id,
                "failed" if summary["failed"] else "success",
                summary["attempted"],
                summary["completed"],
                summary["failed"],
                summary["rows"],
            )
        return summary

    def _upsert(self, rows: Iterable[InstitutionalActivity]) -> int:
        count = 0
        with self.database.transaction() as connection:
            for row in rows:
                statement = sqlite_insert(institutional_activity).values(
                    trade_date=row.trade_date,
                    category="FII/FPI",
                    source="CDSL",
                    buy_value=row.buy_value,
                    sell_value=row.sell_value,
                    net_value=row.net_value,
                    unit="INR crore",
                    raw_json=json.dumps(row.raw, sort_keys=True),
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            institutional_activity.c.trade_date,
                            institutional_activity.c.category,
                            institutional_activity.c.source,
                        ],
                        set_={
                            "buy_value": statement.excluded.buy_value,
                            "sell_value": statement.excluded.sell_value,
                            "net_value": statement.excluded.net_value,
                            "unit": statement.excluded.unit,
                            "raw_json": statement.excluded.raw_json,
                        },
                    )
                )
                count += 1
        return count
