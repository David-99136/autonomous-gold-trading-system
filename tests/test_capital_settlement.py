import copy
from hashlib import sha256
import unittest
from decimal import Decimal
from unittest.mock import Mock

from gold_system.capital_settlement import normalize_full_close, ingest_full_close, SettlementUnverified


class CapitalSettlementTests(unittest.TestCase):
    def setUp(self):
        self.e = dict(session_before={"accountId": "account-canary", "currency": "USD"},
                      session_after={"accountId": "account-canary", "currency": "USD"},
                      expected_reference="ref-canary", expected_deal_id="deal-canary", expected_epic="ETHUSD",
                      positions_before=[{"position": {"dealId": "deal-canary", "currency": "USD", "size": "0.001"},
                                         "market": {"epic": "ETHUSD"}}], positions_after=[],
                      confirmation=dict(dealReference="ref-canary", dealId="deal-canary", epic="ETHUSD",
                                        dealStatus="ACCEPTED", status="CLOSED", size="0.001", profit="-0.00146",
                                        profitCurrency="USD", date="2026-09-20T00:09:37.757Z",
                                        affectedDeals=[{"dealId": "deal-canary", "status": "FULLY_CLOSED"}]))

    def test_verified_close_normalizes_without_identifiers(self):
        result = normalize_full_close(**self.e)
        self.assertEqual(result["realized_pnl"], Decimal("-0.00146"))
        self.assertIsNone(result["extra_fee"])
        self.assertNotIn("canary", repr(result))
        self.assertEqual(result["account_hash"], sha256(b"account-canary").hexdigest())

    def test_observed_naive_time_is_rejected_not_assumed_utc(self):
        self.e["confirmation"]["date"] = "2026-09-20T00:09:37.757"
        with self.assertRaisesRegex(SettlementUnverified, "TIMEZONE_UNVERIFIED"):
            normalize_full_close(**self.e)

    def test_netting_reduce_without_profit_is_not_booked(self):
        self.e["confirmation"].update(status="OPEN", affectedDeals=[])
        self.e["confirmation"].pop("profit")
        ledger = Mock()
        with self.assertRaises(SettlementUnverified):
            ingest_full_close(ledger, **self.e)
        ledger.record_close.assert_not_called()

    def test_account_change_or_identity_conflicts_fail(self):
        for area, key, value in (("session_after", "accountId", "other"),
                                 ("confirmation", "dealReference", "wrong"),
                                 ("confirmation", "epic", "GOLD"),
                                 ("confirmation", "profitCurrency", "EUR")):
            data = copy.deepcopy(self.e)
            data[area][key] = value
            with self.assertRaises(SettlementUnverified):
                normalize_full_close(**data)

    def test_position_still_present_or_wrong_quantity_fail(self):
        self.e["positions_after"] = copy.deepcopy(self.e["positions_before"])
        with self.assertRaises(SettlementUnverified):
            normalize_full_close(**self.e)
        self.e["positions_after"] = []
        self.e["positions_before"][0]["position"]["size"] = "0.002"
        with self.assertRaises(SettlementUnverified):
            normalize_full_close(**self.e)

    def test_invalid_money_or_ambiguous_affected_deals_fail(self):
        for value in (None, True, "NaN", "Infinity"):
            data = copy.deepcopy(self.e)
            data["confirmation"]["profit"] = value
            with self.assertRaises(SettlementUnverified):
                normalize_full_close(**data)
        self.e["confirmation"]["affectedDeals"] *= 2
        with self.assertRaises(SettlementUnverified):
            normalize_full_close(**self.e)

    def test_ingestion_passes_validated_fields_only(self):
        ledger = Mock()
        ledger.record_close.return_value = "event-hash"
        self.e["confirmation"]["token"] = "secret-canary"
        self.assertEqual(ingest_full_close(ledger, **self.e), "event-hash")
        self.assertNotIn("canary", repr(ledger.record_close.call_args))

    def correlated_activity(self):
        self.e["confirmation"].update(date="2026-09-20T00:09:37.757", direction="SELL", level="2625.83")
        self.e["activities"] = [dict(type="POSITION", status="ACCEPTED", dealId="deal-canary", epic="ETHUSD",
                                     dateUTC="2026-09-20T00:09:37.757", details=dict(
                                         dealReference="ref-canary", currency="USD", direction="SELL",
                                         size="0.001", level="2625.83"))]

    def test_official_utc_activity_corroborates_naive_confirmation(self):
        self.correlated_activity()
        result = normalize_full_close(**self.e)
        self.assertEqual(result["occurred_at"].isoformat(), "2026-09-20T00:09:37.757000+00:00")

    def test_reused_reference_alone_is_not_enough(self):
        self.correlated_activity()
        self.e["activities"][0]["dateUTC"] = "2026-09-20T00:09:35.813"
        with self.assertRaises(SettlementUnverified):
            normalize_full_close(**self.e)

    def test_ambiguous_or_wrong_price_activity_is_rejected(self):
        self.correlated_activity()
        self.e["activities"] *= 2
        with self.assertRaises(SettlementUnverified):
            normalize_full_close(**self.e)
        self.correlated_activity()
        self.e["activities"][0]["details"]["level"] = "2625.64"
        with self.assertRaises(SettlementUnverified):
            normalize_full_close(**self.e)
