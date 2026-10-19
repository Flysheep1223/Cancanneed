"""Synthetic regression for records timestamped outside their source shard."""

import contextlib
import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ark_minute_usage import save_aggregation
from ark_usage import ARK_ROOT, write_json
from import_tpm import load_export
from sync_tpm_range import make_summary


class CrossShardTimestampTests(unittest.TestCase):
    def test_global_range_recovers_record_and_deduplicates_cross_shard_copy(self):
        start = 946684800  # Invented fixture: 2000-01-01 UTC.
        model = "model-example"
        first = {
            "uuid": "a", "req_id": "req-a", "account_id": "account-1",
            "model_name": model, "model_version": "v1", "model_endpoint": "ep-example",
            "timestamp": str(start + 5), "input_tokens": "100",
            "output_tokens": "20", "cache_hit_tokens": "50",
        }
        # The second shard returns a distinct record whose timestamp belongs to the first.
        late = {**first, "uuid": "b", "req_id": "req-b", "timestamp": str(start + 56)}
        with tempfile.TemporaryDirectory(dir=ARK_ROOT / "data") as temporary:
            root = Path(temporary)
            manifest = {"start": start, "end": start + 120, "models": [model], "windows": []}
            for index, rows in enumerate(([first], [first, late])):
                directory = root / f"window-{index}"
                directory.mkdir()
                request = {
                    "StartTime": start + index * 60, "EndTime": start + (index + 1) * 60,
                    "Filters": [{"Key": "model_name", "Values": [model]}],
                }
                write_json(directory / "request.json", {"body": request})
                write_json(directory / "task.json", {"Result": {"Task": {
                    "Id": f"offline-{index}", "Status": "Succeeded",
                    "ExportCount": len(rows), "TotalCount": len(rows),
                }}})
                with (directory / "records.csv").open("w", newline="") as output:
                    writer = csv.DictWriter(output, fieldnames=list(first))
                    writer.writeheader()
                    writer.writerows(rows)
                with contextlib.redirect_stdout(io.StringIO()):
                    save_aggregation(directory)
                manifest["windows"].append({"directory": directory.name, "status": "imported"})

            combined = make_summary(root, manifest)
            self.assertEqual(combined["validation"]["csv_rows"], 3)
            self.assertEqual(combined["validation"]["included_record_count"], 2)
            self.assertEqual(combined["validation"]["duplicate_records_skipped"], 1)
            self.assertEqual(combined["validation"]["records_outside_source_shard"], 2)
            self.assertEqual(combined["rows"][0]["total_tokens"], 240)
            self.assertEqual(len(combined["rows"]), 1)

            source_summary, _, records = load_export(
                root / "window-1", "account-1", (start, start + 120),
            )
            self.assertEqual(len(records), 2)
            # Importing uses global inclusion while preserving actual export coverage.
            from datetime import datetime
            self.assertEqual(
                int(datetime.fromisoformat(source_summary["window"]["start_inclusive"]).timestamp()),
                start + 60,
            )


if __name__ == "__main__":
    unittest.main()
