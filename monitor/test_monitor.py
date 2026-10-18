"""Focused checks for catalogue parity, response handling, and page history."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

import monitor


class MonitorTests(unittest.TestCase):
    def test_catalogue_covers_active_variants(self):
        checks = monitor.catalogue()
        ids = {check.id for check in checks}
        self.assertEqual(len(checks), len(ids))
        self.assertEqual(len(checks), 51)
        self.assertEqual({check.id for check in checks if check.id.startswith("regional-")}, {f"regional-{region.lower()}" for region in monitor.REGIONS})
        self.assertEqual({check.id for check in checks if check.id.startswith("warning-ei")}, {f"warning-{code.lower()}" for code in monitor.COUNTY_CODES.values()})
        self.assertNotIn("radar", ids)  # dormant radar mode
        self.assertNotIn("archive-48h", ids)  # dormant archive mode

    def test_warning_control_characters_are_tolerated(self):
        data = b'[{"description":"A\tB\nC","id":1}]'
        self.assertEqual(monitor.validate("warnings", data, "application/json")[0]["description"], "A\tB\nC")

    def test_archive_and_tile_urls_come_from_current_metadata(self):
        archive = b'<long-range-radar maps="[&quot;image.png&quot;]" times="[&quot;now&quot;]" legend="">'
        images = monitor.validate("archive", archive, "text/html")
        tile = monitor.derived_url(monitor.Check("tile", "Tile", "", "image", "rainfall"), [{"src": "abc", "modifiedTime": 123, "server": "https://gdal.met.ie"}])
        image = monitor.derived_url(monitor.Check("image", "Image", "", "image", "archive"), images)
        self.assertEqual(tile, "https://gdal.met.ie/api/maps/rainfall/abc/30/21/6/123")
        self.assertEqual(image, "https://www.met.ie/images/radar/image.png")

    def test_history_keeps_twenty_and_dashboard_shows_failure(self):
        checks = [monitor.Check("one", "One", "https://example.test", "json")]
        history = {"runs": []}
        for number in range(21):
            history = monitor.append_history(history, {"one": {"ok": number != 20, "status": 503 if number == 20 else 200, "ms": 10, "error": "HTTP 503" if number == 20 else None, "url": checks[0].url}}, f"2026-10-18T{number:02d}:00:00+00:00")
        self.assertEqual(len(history["runs"]), 20)
        self.assertEqual(history["runs"][0]["at"], "2026-10-18T01:00:00+00:00")
        page = monitor.render_dashboard(history, checks)
        self.assertIn("HTTP 503", page)
        self.assertIn("1 failing", page)
        self.assertEqual(page.count('class="pip '), 20)

    def test_injected_failure_is_recorded(self):
        checks = [monitor.Check("one", "One", "https://example.test", "json")]
        with patch.object(monitor, "request", return_value=({"ok": True, "status": 200, "ms": 7, "error": None, "url": checks[0].url}, {"fine": True})):
            results = monitor.run_checks(checks, fail_id="one")
        self.assertFalse(results["one"]["ok"])
        self.assertEqual(results["one"]["error"], "Injected test failure")

    def test_cli_publishes_success_and_injected_failure(self):
        class Response:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b'{"fine":true}'

        def open_stub(request, timeout):
            if request.full_url.endswith("/warnings"):
                raise HTTPError(request.full_url, 404, "missing", {}, None)
            return Response()

        checks = [monitor.Check("json", "JSON", "https://example.test/ok", "json"), monitor.Check("warning", "Warning", "https://example.test/warnings", "warnings")]
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "history.json"
            site = Path(directory) / "site"
            argv = ["monitor.py", "--history", str(history), "--site", str(site)]
            with patch.object(monitor, "catalogue", return_value=checks), patch.object(monitor.urllib.request, "urlopen", side_effect=open_stub), patch.object(sys, "argv", argv):
                self.assertEqual(monitor.main(), 0)
            self.assertTrue((site / "index.html").exists())
            self.assertTrue(json.loads(history.read_text())["runs"][0]["results"]["warning"]["ok"])
            with patch.object(monitor, "catalogue", return_value=checks), patch.object(monitor.urllib.request, "urlopen", side_effect=open_stub), patch.object(sys, "argv", argv + ["--fail-id", "json"]):
                self.assertEqual(monitor.main(), 1)
            saved = json.loads(history.read_text())
            self.assertEqual(len(saved["runs"]), 2)
            self.assertIn("1 failing", (site / "index.html").read_text())


if __name__ == "__main__":
    unittest.main()
