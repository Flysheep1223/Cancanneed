"""Offline checks using invented fixtures, never exported account data."""

import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from volcengine.Credentials import Credentials

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ark_usage


def response_document(names, types, rows):
    return {"Result": {
        "Fields": [{"Name": name, "Type": kind} for name, kind in zip(names, types)],
        "Data": rows, "DataCount": len(rows),
    }}


class UsageContractTests(unittest.TestCase):
    def test_request_filters_and_empty_dimension_values(self):
        args = ark_usage.parser().parse_args([
            "--start", "2000-01-01", "--end", "2000-01-01",
            "--endpoint", "ep-example", "--api-key-id", "resource-example",
            "--dimension", "ModelName", "--dimension", "ModelEndpoint",
        ])
        body = ark_usage.build_payload(args)
        self.assertEqual(body["QueryInterval"], "Hour")
        self.assertEqual(body["Filters"], [
            {"Key": "ModelEndpoint", "Values": ["ep-example"]},
            {"Key": "ApikeyID", "Values": ["resource-example"]},
            {"Key": "ModelName", "Values": []},
        ])

    def test_reject_reversed_dates(self):
        args = ark_usage.parser().parse_args([
            "--start", "2000-01-02", "--end", "2000-01-01",
        ])
        with self.assertRaises(ark_usage.UsageError):
            ark_usage.build_payload(args)

    def test_synthetic_rows_map_columns_and_sort_shanghai_hours(self):
        doc = response_document(
            ["AccountID", "ModelEndpoint", "Day", "Hour", "InputTokens", "OutputTokens",
             "ImageCount", "TotalTokens", "ReqCnt"],
            ["BIGINT", "STRING", "DATE", "STRING", "BIGINT", "BIGINT", "BIGINT", "BIGINT", "BIGINT"],
            [
                ["1", "ep-example", "2000-01-02", "16", "100", "20", "0", "120", "1"],
                ["1", "ep-example", "2000-01-01", "14", "200", "40", "0", "240", "2"],
            ],
        )
        rows = ark_usage.normalize_response(doc, "Hour")
        self.assertEqual(rows[0]["bucket_start"], "2000-01-01T14:00:00+08:00")
        self.assertEqual(rows[1]["values"]["TotalTokens"], 120)
        self.assertEqual(rows[1]["values"]["AccountID"], "1")

    def test_reordered_columns_bigints_unknown_and_missing_metrics(self):
        doc = response_document(
            ["TotalTokens", "Hour", "FutureField", "Day", "AccountID"],
            ["BIGINT", "STRING", "STRING", "DATE", "BIGINT"],
            # 2**53 + 1 and + 3 exercise precision; these are invented IDs/counts.
            [["9007199254740993", "03", "preserved", "2000-01-01", "9007199254740995"]],
        )
        row = ark_usage.normalize_response(doc, "Hour")[0]
        self.assertEqual(row["values"]["TotalTokens"], 9007199254740993)
        self.assertEqual(row["values"]["AccountID"], "9007199254740995")
        self.assertEqual(row["values"]["FutureField"], "preserved")
        self.assertNotIn("InputTokens", row["values"])
        self.assertEqual(row["bucket_start"], "2000-01-01T03:00:00+08:00")

    def test_day_does_not_require_hour_and_keeps_null_as_unknown(self):
        doc = response_document(
            ["Day", "InputTokens"], ["DATE", "BIGINT"], [["2000-01-01", None]],
        )
        row = ark_usage.normalize_response(doc, "Day")[0]
        self.assertEqual(row["bucket_start"], "2000-01-01T00:00:00+08:00")
        self.assertIsNone(row["values"]["InputTokens"])
        with self.assertRaises(ark_usage.UsageError):
            ark_usage.normalize_response(doc, "Hour")

    def test_empty_result_is_not_fabricated_zero_usage(self):
        self.assertEqual(ark_usage.normalize_response(response_document([], [], []), "Hour"), [])

    def test_fail_explicitly_on_malformed_schema(self):
        valid = response_document(["Day", "Hour"], ["DATE", "STRING"], [["2000-01-01", "16"]])
        wrong_count = {**valid, "Result": {**valid["Result"], "DataCount": 100}}
        wrong_width = {**valid, "Result": {**valid["Result"], "Data": [["2000-01-01"]]}}
        duplicate = response_document(["Day", "Day"], ["DATE", "DATE"], [["2000-01-01"] * 2])
        bad_hour = response_document(["Day", "Hour"], ["DATE", "STRING"], [["2000-01-01", "24"]])
        float_count = response_document(
            ["Day", "TotalTokens"], ["DATE", "BIGINT"], [["2000-01-01", 1.25]],
        )
        for document in (wrong_count, wrong_width, duplicate, bad_hour, float_count):
            with self.subTest(document=document), self.assertRaises(ark_usage.UsageError):
                ark_usage.normalize_response(document, "Hour")


class UsageTransportTests(unittest.TestCase):
    credentials = Credentials("offline-example-ak", "offline-example-sk", "ark", "cn-beijing")

    def test_sdk_signs_exact_utf8_body_that_requests_will_send(self):
        payload = {"StartTime": "2000-01-01", "ProjectName": "测试项目"}
        with patch.object(ark_usage.SignerV4, "get_current_format_date", return_value="20000102T000000Z"):
            signed = ark_usage.sign_request(payload, self.credentials)
        prepared = requests.Request(
            signed.method, signed.build(), headers=signed.headers,
            data=signed.body.encode("utf-8"),
        ).prepare()
        self.assertEqual(json.loads(prepared.body), payload)
        self.assertEqual(prepared.headers["X-Content-Sha256"], hashlib.sha256(prepared.body).hexdigest())
        self.assertEqual(prepared.headers["Host"], "ark.cn-beijing.volcengineapi.com")
        self.assertIn("Action=GetInferenceUsage&Version=2024-01-01", prepared.url)
        self.assertIn("/20000102/cn-beijing/ark/request", prepared.headers["Authorization"])
        self.assertIn("SignedHeaders=content-type;host;x-content-sha256;x-date", prepared.headers["Authorization"])
        self.assertNotIn("offline-example-sk", str(prepared.headers))

    @staticmethod
    def http_response(status, document, headers=None):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(document).encode("utf-8")
        response.headers.update(headers or {})
        return response

    def test_429_retry_preserves_response_and_disables_redirects(self):
        document = response_document([], [], [])
        session = Mock()
        session.post.side_effect = [
            self.http_response(429, {}, {"Retry-After": "2"}),
            self.http_response(200, document),
        ]
        with patch("ark_usage.requests.Session") as session_class, patch("ark_usage.time.sleep") as sleep:
            session_class.return_value.__enter__.return_value = session
            raw, parsed = ark_usage.query_usage({}, self.credentials)
        self.assertEqual(parsed, document)
        self.assertEqual(json.loads(raw), document)
        self.assertGreaterEqual(sleep.call_args.args[0], 2)
        self.assertEqual(session.post.call_count, 2)
        self.assertFalse(session.trust_env)
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
        self.assertEqual(session.post.call_args.kwargs["timeout"], (5, 30))

    def test_auth_error_not_retried_or_echoed(self):
        session = Mock()
        session.post.return_value = self.http_response(400, {
            "ResponseMetadata": {
                "RequestId": "request-example",
                "Error": {"Code": "InvalidCredential", "Message": "do-not-echo-server-message"},
            },
        })
        with patch("ark_usage.requests.Session") as session_class, patch("ark_usage.time.sleep") as sleep:
            session_class.return_value.__enter__.return_value = session
            with self.assertRaises(ark_usage.UsageError) as caught:
                ark_usage.query_usage({}, self.credentials)
        self.assertIn("InvalidCredential", str(caught.exception))
        self.assertIn("request-example", str(caught.exception))
        self.assertNotIn("do-not-echo-server-message", str(caught.exception))
        self.assertEqual(session.post.call_count, 1)
        sleep.assert_not_called()

    def test_network_retries_are_bounded(self):
        session = Mock()
        session.post.side_effect = requests.Timeout()
        with patch("ark_usage.requests.Session") as session_class, patch("ark_usage.time.sleep"):
            session_class.return_value.__enter__.return_value = session
            with self.assertRaises(ark_usage.UsageError):
                ark_usage.query_usage({}, self.credentials)
        self.assertEqual(session.post.call_count, 3)

    def test_long_retry_after_stops_instead_of_retrying_early(self):
        with self.assertRaises(ark_usage.UsageError):
            ark_usage.retry_delay(0, "120")


if __name__ == "__main__":
    unittest.main()
