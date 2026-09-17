import csv
import io
import unittest
import zipfile
from datetime import date
from unittest.mock import Mock

from market_data.sources import (
    BSESource,
    NSESource,
    parse_bse_bhavcopy,
    parse_nse_bhavcopy,
)


def zipped_csv(headers, rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("fixture.csv", buffer.getvalue())
    return output.getvalue()


class SourceParsingTests(unittest.TestCase):
    def test_parse_nse_legacy_archive(self):
        data = zipped_csv(
            ["SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "TOTTRDQTY", "ISIN"],
            [
                {
                    "SYMBOL": "ABC",
                    "SERIES": "EQ",
                    "OPEN": "10",
                    "HIGH": "12",
                    "LOW": "9",
                    "CLOSE": "11",
                    "TOTTRDQTY": "1000",
                    "ISIN": "INE123A01010",
                }
            ],
        )
        records = parse_nse_bhavcopy(data, date(2024, 1, 2))
        self.assertEqual(records[0]["symbol"], "ABC")
        self.assertEqual(records[0]["volume"], 1000)

    def test_parse_bse_udiff_archive(self):
        data = zipped_csv(
            ["FinInstrmId", "TckrSymb", "SctySrs", "OpnPric", "HghPric", "LwPric", "ClsPric", "TtlTradgVol"],
            [
                {
                    "FinInstrmId": "500001",
                    "TckrSymb": "ABC",
                    "SctySrs": "A",
                    "OpnPric": "20",
                    "HghPric": "21",
                    "LwPric": "19",
                    "ClsPric": "20.5",
                    "TtlTradgVol": "50",
                }
            ],
        )
        records = parse_bse_bhavcopy(data, date(2024, 7, 9))
        self.assertEqual(records[0]["scrip_code"], "500001")
        self.assertEqual(records[0]["close"], 20.5)

    def test_sources_use_mocked_official_http_fixtures(self):
        nse_client = Mock()
        nse_client.get.return_value.content = (
            b"SYMBOL,NAME OF COMPANY,SERIES,ISIN NUMBER\n"
            b"ABC,ABC Limited,EQ,INE123A01010\n"
        )
        self.assertEqual(NSESource(nse_client).fetch_master()[0]["symbol"], "ABC")

        bse_client = Mock()
        bse_client.get.return_value.json.return_value = {
            "Table": [
                {
                    "SCRIP_ID": "ABC",
                    "SCRIP_CD": "500001",
                    "SCRIP_NAME": "ABC Limited",
                    "ISIN_NO": "INE123A01010",
                    "GROUP_NAME": "A",
                }
            ]
        }
        self.assertEqual(BSESource(bse_client).fetch_master()[0]["scrip_code"], "500001")
