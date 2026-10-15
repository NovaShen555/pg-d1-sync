from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from app.d1_client import D1Client, D1Error, D1Statement


class TestD1Client(unittest.TestCase):
    def setUp(self) -> None:
        self.client = D1Client(
            account_id="account", database_id="database", api_token="test-secret",
            api_base="https://example.test/client/v4",
        )

    def test_query_uses_expected_endpoint_and_string_params(self) -> None:
        data = {"success": True, "result": [{"success": True, "results": [{"id": 3}]}]}
        captured = {}

        def fake_urlopen(req, timeout):
            captured["request"] = req
            captured["timeout"] = timeout
            return io.BytesIO(json.dumps(data).encode())

        with patch("app.d1_client.request.urlopen", side_effect=fake_urlopen):
            rows = self.client.query("SELECT * FROM t WHERE id=?", ("3",))
        self.assertEqual(rows, [{"id": 3}])
        self.assertTrue(captured["request"].full_url.endswith("/accounts/account/d1/database/database/query"))
        self.assertEqual(json.loads(captured["request"].data)["params"], ["3"])
        self.assertEqual(captured["timeout"], 30.0)

    def test_partial_batch_must_not_report_success(self) -> None:
        data = {"success": True, "result": [{"success": True, "results": []}]}
        with patch("app.d1_client.request.urlopen", return_value=io.BytesIO(json.dumps(data).encode())):
            with self.assertRaisesRegex(D1Error, "expected 2"):
                self.client.batch([D1Statement("SELECT 1"), D1Statement("SELECT 2")])

    def test_statement_failure_must_not_report_success(self) -> None:
        data = {"success": True, "result": [{"success": False, "error": "bad SQL"}]}
        with patch("app.d1_client.request.urlopen", return_value=io.BytesIO(json.dumps(data).encode())):
            with self.assertRaisesRegex(D1Error, "bad SQL"):
                self.client.query("BAD")

    def test_token_is_only_in_authorization_header(self) -> None:
        data = {"success": True, "result": [{"success": True, "results": []}]}
        with patch("app.d1_client.request.urlopen", return_value=io.BytesIO(json.dumps(data).encode())) as opened:
            self.client.query("SELECT 1")
        req = opened.call_args.args[0]
        self.assertNotIn("test-secret", req.full_url)
        self.assertNotIn(b"test-secret", req.data)
        self.assertEqual(req.get_header("Authorization"), "Bearer test-secret")


if __name__ == "__main__":
    unittest.main()
