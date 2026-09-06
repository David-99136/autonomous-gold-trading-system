import unittest
import io
from urllib.error import HTTPError
from unittest.mock import Mock
from email.message import Message
from gold_system.capital import CapitalDemo, NoRedirect
from gold_system.validation import performance, qualification, operating_profit
from gold_system.core import D


class CapitalTests(unittest.TestCase):
    def test_401_reports_only_error_code(self):
        client = CapitalDemo()
        client._opener = Mock()
        client._opener.open.side_effect = HTTPError(client.BASE + '/session', 401, 'Unauthorized', {},
            io.BytesIO(b'{"errorCode":"error.invalid.details","password":"DO_NOT_PRINT"}'))
        with self.assertRaises(RuntimeError) as caught:
            client.login(dict(api_key='fake-key', identifier='fake-email', password='fake-password'))
        self.assertIn('error.invalid.details', str(caught.exception))
        self.assertNotIn('DO_NOT_PRINT', str(caught.exception))
        self.assertFalse(client._headers)

    def test_demo_only_discovery_and_no_raw_key_after_login(self):
        client = CapitalDemo()
        calls = []
        def request(method, path, body=None):
            calls.append((method, path))
            headers = Message()
            headers["CST"] = "fake-session"
            headers["X-SECURITY-TOKEN"] = "fake-account"
            return {"markets": []}, headers
        client._request = request
        client.login(dict(api_key="fake-key", identifier="fake-user", password="fake-password"))
        client.discover()
        self.assertEqual(calls, [("POST", "/session"), ("GET", "/markets?searchTerm=Gold")])
        self.assertNotIn("X-CAP-API-KEY", client._headers)
        self.assertIn("demo-api", client.BASE)
        client.close()
        self.assertFalse(client._headers)

    def test_error_payloads_cannot_echo_secrets(self):
        for body in (b'<html>SECRET</html>', b'{"errorCode":"SECRET"}',
                     b'{"errorCode":"error.SECRET"}', b'[]'):
            client = CapitalDemo()
            client._opener = Mock()
            client._opener.open.side_effect = HTTPError(client.BASE+'/session', 401, 'Unauthorized', {}, io.BytesIO(body))
            with self.assertRaises(RuntimeError) as caught:
                client.login(dict(api_key='fake-key', identifier='fake-email', password='SECRET'))
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertIn('errorCode=UNAVAILABLE', str(caught.exception))

    def test_redirect_refused(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, None, {}, "https://other.example"))

    def test_missing_tokens_clear_auth(self):
        client = CapitalDemo()
        client._request = lambda *args: ({}, {})
        with self.assertRaises(RuntimeError):
            client.login(dict(api_key="fake", identifier="fake", password="fake"))
        self.assertFalse(client._headers)

    def test_equity_includes_floating_drawdown(self):
        m = performance([D(20), D(-10)], [D(100), D(80), D(110)])
        self.assertEqual(m["profit_factor"], D(2))
        self.assertEqual(m["maximum_drawdown"], D("0.2"))

    def test_insufficient_data_never_passes(self):
        self.assertFalse(qualification({}, None, 0, {})["numeric_gates_passed"])
        self.assertFalse(qualification({}, None, 0, {})["live_unlocked"])

    def test_costs_not_double_counted(self):
        self.assertEqual(operating_profit(D(100), D(2), D(3), D(4), D(5)), D(86))
