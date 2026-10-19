"""Offline accounting tests; all records and timestamps are invented."""

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ark_minute_usage as minute
from volcengine.Credentials import Credentials


class MinuteAggregationTests(unittest.TestCase):
    # 2000-01-01 08:00:00+08:00, a synthetic whole-minute fixture.
    start = 946684800
    columns = [
        "uuid", "req_id", "account_id", "model_name", "timestamp",
        "input_tokens", "output_tokens", "cache_hit_tokens",
    ]

    def record(self, uuid="record-1", req_id="request-1", **values):
        return {
            "uuid": uuid, "req_id": req_id, "account_id": "9007199254740993",
            "model_name": "model-example", "timestamp": str(self.start),
            "input_tokens": "100", "output_tokens": "20", "cache_hit_tokens": "50",
            **values,
        }

    def aggregate(self, rows, total_count=None):
        minute.ARK_ROOT.joinpath("data").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=minute.ARK_ROOT / "data") as directory:
            path = Path(directory) / "records.csv"
            with path.open("w", newline="", encoding="utf-8-sig") as output:
                writer = csv.DictWriter(output, fieldnames=self.columns)
                writer.writeheader()
                writer.writerows(rows)
            request = {
                "StartTime": self.start, "EndTime": self.start + 120,
                "Filters": [{"Key": "model_name", "Values": ["model-example"]}],
            }
            task = {
                "Id": "offline-task", "Status": "Succeeded",
                "ExportCount": len(rows), "TotalCount": len(rows) if total_count is None else total_count,
            }
            return minute.aggregate_records(path, request, task)

    def test_distinct_records_of_one_request_are_summed_but_exact_duplicate_is_not(self):
        first = self.record()
        doc = self.aggregate([first, first, self.record(uuid="record-2")])
        row = doc["rows"][0]
        self.assertEqual(row["input_tokens"], 200)
        self.assertEqual(row["cache_hit_tokens"], 100)
        self.assertEqual(row["total_tokens"], 240)  # Cached tokens are not added twice.
        self.assertEqual(row["record_count"], 2)
        self.assertEqual(row["request_count"], 1)
        self.assertEqual(doc["validation"]["duplicate_records_skipped"], 1)
        self.assertFalse(doc["scope"]["full_account_coverage_confirmed"])

    def test_half_open_window_and_shanghai_minute_buckets(self):
        rows = [
            self.record(uuid=str(offset), timestamp=str(self.start + offset))
            for offset in (-1, 0, 59, 60, 119, 120)
        ]
        doc = self.aggregate(rows)
        self.assertEqual(doc["validation"]["outside_window_skipped"], 2)
        self.assertEqual([row["minute_start"] for row in doc["rows"]], [
            "2000-01-01T08:00:00+08:00", "2000-01-01T08:01:00+08:00",
        ])
        self.assertEqual([row["total_tokens"] for row in doc["rows"]], [240, 240])

    def test_integer_precision_and_missing_values(self):
        doc = self.aggregate([self.record(input_tokens="9007199254740993")])
        self.assertEqual(doc["rows"][0]["total_tokens"], 9007199254741013)
        self.assertEqual(doc["rows"][0]["account_id"], "9007199254740993")
        with self.assertRaises(minute.UsageError):
            self.aggregate([self.record(input_tokens="")])

    def test_reject_truncation_and_conflicting_uuid(self):
        with self.assertRaises(minute.UsageError):
            self.aggregate([self.record()], total_count=100001)
        with self.assertRaises(minute.UsageError):
            self.aggregate([self.record(), self.record(output_tokens="21")])

    def test_empty_result_does_not_fabricate_zero_model_totals(self):
        doc = self.aggregate([])
        self.assertEqual(doc["rows"], [])
        self.assertEqual(doc["model_totals"], [])
        self.assertEqual(doc["scope"]["models_without_exported_records"], ["model-example"])

    def test_create_signature_covers_export_action(self):
        session = Mock()
        response = session.post.return_value
        response.status_code = 200
        response.json.return_value = {"Result": {"Task": {"Id": "offline-task"}}}
        credentials = Credentials("offline-ak", "offline-sk", "ark", "cn-beijing", "offline-sts")
        body = {"StartTime": self.start}
        minute.call_api(session, credentials, "CreateRecordExportTask", body)
        self.assertEqual(session.post.call_count, 1)
        self.assertIn("Action=CreateRecordExportTask", session.post.call_args.args[0])
        self.assertEqual(json.loads(session.post.call_args.kwargs["data"]), body)
        self.assertEqual(session.post.call_args.kwargs["headers"]["X-Security-Token"], "offline-sts")


if __name__ == "__main__":
    unittest.main()
