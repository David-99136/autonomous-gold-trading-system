import unittest
from unittest.mock import AsyncMock

from gold_system.account_history_probe import collect_history, history_query, scrub


class HistoryProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_session_only_gets_and_scrubs_secrets(self):
        client = AsyncMock()
        client.session.return_value = {"accountId": "private", "currency": "USD"}
        client._request.side_effect = [({"transactions": [], "token": "secret"}, {}),
                                       ({"activities": []}, {})]
        result = await collect_history(client, "2026-09-20T08:09:00+08:00", "2026-09-20T08:11:00+08:00")
        self.assertNotIn("private", str(result))
        self.assertNotIn("secret", str(result))
        self.assertFalse(result["accounting_verified"])
        for call in client._request.call_args_list:
            self.assertEqual(call.args[0], "GET")
            self.assertIn("2026-09-20T00%3A09%3A00", call.args[1])
        client.login.assert_not_called()

    async def test_account_switch_fails(self):
        client = AsyncMock()
        client.session.side_effect = [{"accountId": "one", "currency": "USD"},
                                      {"accountId": "two", "currency": "USD"}]
        client._request.side_effect = [({"transactions": []}, {}), ({"activities": []}, {})]
        with self.assertRaisesRegex(ValueError, "ACCOUNT_CHANGED"):
            await collect_history(client, "2026-09-20T00:09:00Z", "2026-09-20T00:11:00Z")

    def test_invalid_window_or_naive_time_rejected(self):
        for start, end in (("2026-09-20", "2026-09-21"),
                           ("2026-09-20T00:00:00Z", "2026-09-22T00:00:00Z")):
            with self.assertRaises(ValueError):
                history_query(start, end)

    def test_nested_secret_scrubbing(self):
        self.assertEqual(scrub({"rows": [{"CST": "s", "size": 1}]}), {"rows": [{"size": 1}]})
