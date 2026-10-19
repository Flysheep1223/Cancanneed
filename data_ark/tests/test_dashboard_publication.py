"""Ensure private batch metadata never changes the publishable dashboard."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which("node"), "Dashboard generation requires Node.js")
class DashboardPublicationTests(unittest.TestCase):
    def test_private_window_only_updates_ignored_runtime_directory(self):
        project = Path(__file__).resolve().parents[2]
        (project / ".runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=project / ".runtime") as temporary:
            root = Path(temporary)
            script = root / "data_ark/scripts/build-tpm-dashboard.mjs"
            script.parent.mkdir(parents=True)
            shutil.copy2(project / "data_ark/scripts/build-tpm-dashboard.mjs", script)
            subprocess.run(["node", str(script)], check=True, capture_output=True)
            template = root / "data_ark/grafana/dashboards/ark-model-tpm.json"
            public_content = template.read_bytes()
            public = json.loads(public_content)
            self.assertEqual(public["time"], {"from": "now-24h", "to": "now"})
            self.assertEqual(len(public["panels"]), 2)
            self.assertNotIn("current", public["templating"]["list"][0])

            # Entirely invented input; sentinel fields must not enter either JSON.
            summary = root / "synthetic-input.json"
            summary.write_text(json.dumps({
                "window": {
                    "start_inclusive": "2000-01-01T00:00:00Z",
                    "end_exclusive": "2000-01-02T00:00:00Z",
                },
                "scope": {"account_ids": ["synthetic-private-account"]},
                "rows": [{"request_id": "synthetic-private-request"}],
            }))
            subprocess.run(["node", str(script), str(summary)], check=True, capture_output=True)
            self.assertEqual(template.read_bytes(), public_content)
            runtime = root / ".runtime/ark-dashboards/ark-model-tpm.json"
            private = json.loads(runtime.read_text())
            self.assertEqual(private["time"], {
                "from": "2000-01-01T00:00:00.000Z",
                "to": "2000-01-02T00:00:00.000Z",
            })
            self.assertNotIn("synthetic-private", runtime.read_text())
            subprocess.run(["node", str(script)], check=True, capture_output=True)
            self.assertEqual(json.loads(runtime.read_text()), private)


if __name__ == "__main__":
    unittest.main()
