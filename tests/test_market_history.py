import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from gold_system.core import D, Rejected
from gold_system.market import MarketRules
from gold_system.history import download, audit_download


def fixture(now):
    return {"instrument": {"epic": "GOLD", "type": "COMMODITIES", "expiry": "-", "currency": "USD",
            "marginFactor": 5, "marginFactorUnit": "PERCENTAGE",
            "openingHours": {"mon": ["00:00 - 18:30", "22:00 - 00:00"],
                "tue": ["00:00 - 20:59", "22:00 - 00:00"], "wed": [], "thu": [], "fri": [], "sat": [], "sun": [], "zone": "UTC"},
            "overnightFee": {"swapChargeTimestamp": int((now+timedelta(hours=8)).timestamp()*1000)}},
            "dealingRules": {"minStepDistance": {"unit": "POINTS", "value": .01},
                "minStopOrProfitDistance": {"unit": "PERCENTAGE", "value": .001},
                "minDealSize": {"value": .01}, "minSizeIncrement": {"value": .01}, "maxDealSize": {"value": 50000}},
            "snapshot": {"marketStatus": "TRADEABLE", "marketModes": ["REGULAR"]}}


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 7, 17, tzinfo=timezone.utc)
        self.payload = fixture(self.now)
        self.rules = MarketRules(self.payload, self.now)

    def test_percentage_stop_rounded_to_tick(self):
        self.assertEqual(self.rules.distance("minStopOrProfitDistance", D("4430.08")), D("0.05"))
        self.assertEqual(self.rules.contract(D(4430), D(1)).margin_rate, D("0.05"))
        with self.assertRaises(Rejected):
            self.rules.contract(D(4430), None)

    def test_early_close_precedes_funding(self):
        self.assertEqual(self.rules.boundary(self.now), self.now.replace(hour=18, minute=30))
        self.assertTrue(self.rules.entry_status(self.now))

    def test_midnight_is_not_close(self):
        now = self.now.replace(hour=23)
        rules = MarketRules(fixture(now), now)
        self.assertEqual(rules.boundary(now), now+timedelta(hours=8))

    def test_closed_and_stale(self):
        payload = copy.deepcopy(self.payload)
        payload["snapshot"]["marketStatus"] = "CLOSED"
        self.assertFalse(MarketRules(payload, self.now).entry_status(self.now))
        with self.assertRaises(Rejected):
            self.rules.boundary(self.now+timedelta(minutes=6))

    def test_stale_funding_requires_refresh(self):
        self.payload["instrument"]["overnightFee"]["swapChargeTimestamp"] = int(self.now.timestamp()*1000)
        with self.assertRaises(Rejected):
            self.rules.boundary(self.now)


class HistoryTests(unittest.TestCase):
    def test_offline_integrity_rebuild_detects_capture_changes(self):
        start = datetime(2026, 9, 4, tzinfo=timezone.utc)
        class API:
            def prices(self, *args):
                return {"prices": [{"snapshotTimeUTC": start.isoformat()}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/"capture"
            download(API(), "GOLD", start, start+timedelta(minutes=2), root)
            original = {p.name: p.read_text(encoding="utf-8") for p in root.iterdir()}
            self.assertEqual(audit_download(root)["rows"], 1)
            self.assertFalse(audit_download(root)["qualified"])
            cases = [("page-0000.json", "{}"), ("prices.json", "[]")]
            for change in ({"rows": 2}, {"pages": []}, {"gaps_unclassified": [{}]}):
                data = json.loads(original["manifest.json"])
                data.update(change)
                cases.append(("manifest.json", json.dumps(data)))
            data = json.loads(original["manifest.json"])
            data["pages"][0]["file"] = "../outside.json"
            cases.append(("manifest.json", json.dumps(data)))
            for filename, content in cases:
                with self.subTest(filename=filename, content=content):
                    (root/filename).write_text(content, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        audit_download(root)
                    (root/filename).write_text(original[filename], encoding="utf-8")

    def test_page_overlap_and_manifest(self):
        start = datetime(2026, 9, 4, 0, tzinfo=timezone.utc)
        class API:
            def prices(self, epic, resolution, left, right):
                a, b = datetime.fromisoformat(left), datetime.fromisoformat(right)
                rows = []
                while a <= b:
                    rows.append({"snapshotTimeUTC": a.isoformat()})
                    a += timedelta(minutes=1)
                return {"prices": rows}
        with tempfile.TemporaryDirectory() as tmp:
            result = download(API(), "GOLD", start, start+timedelta(minutes=1000), Path(tmp)/"capture")
            self.assertEqual(result["rows"], 1000)
            self.assertEqual(len(result["pages"]), 2)
            self.assertEqual(result["gaps_unclassified"], [])
            self.assertFalse(result["qualified"])
            self.assertEqual(audit_download(Path(tmp)/"capture")["rows"], 1000)

    def test_empty_response_not_fabricated(self):
        class API:
            def prices(self, *args):
                return {"prices": []}
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            result = download(API(), "GOLD", now, now+timedelta(minutes=2), Path(tmp)/"empty")
            self.assertEqual(result["rows"], 0)
            self.assertIsNone(result["first"])
