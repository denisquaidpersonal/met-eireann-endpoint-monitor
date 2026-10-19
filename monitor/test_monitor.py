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
        self.assertEqual(len(checks), 45)
        self.assertEqual({check.id for check in checks if check.id.startswith("regional-")}, {f"regional-{region.lower()}" for region in monitor.REGIONS})
        self.assertEqual({check.id for check in checks if check.id.startswith("supplement-")}, {f"supplement-{region.lower()}" for region in monitor.SUPPLEMENT_REGIONS})
        self.assertEqual({check.id for check in checks if check.id.startswith("warning-ei")}, {f"warning-{code.lower()}" for code in monitor.COUNTY_CODES.values()})
        self.assertTrue({"warnings-all", "coastal", "sea-area", "inland-lake", "uv", "supplement-dublin", "radar", "archive-48h"}.isdisjoint(ids))

    def test_warning_control_characters_are_tolerated(self):
        data = b'[{"description":"A\tB\nC","id":1}]'
        self.assertEqual(monitor.validate("warnings", data, "application/json")[0]["description"], "A\tB\nC")

    def test_warning_http_404_is_down(self):
        check = monitor.Check("warning", "Warning", "https://example.test/warnings", "warnings")
        with patch.object(monitor.urllib.request, "urlopen", side_effect=HTTPError(check.url, 404, "missing", {}, None)):
            result, _ = monitor.request(check)
        self.assertEqual(result["status"], 404)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "HTTP 404")

        class Response:
            status = 404
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b"[]"

        with patch.object(monitor.urllib.request, "urlopen", return_value=Response()):
            result, _ = monitor.request(check)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "HTTP 404")

    def test_old_404_results_and_unused_checks_are_removed(self):
        checks = [monitor.Check("warning", "Warning", "https://example.test/warnings", "warnings")]
        history = {"version": 1, "updated_at": "2026-10-18T08:00:00Z", "runs": [{"at": "2026-10-18T08:00:00Z", "results": {
            "warning": {"ok": True, "status": 404, "ms": 10, "error": None},
            "inland-lake": {"ok": False, "status": 404, "ms": 10, "error": "HTTP 404"},
        }}]}
        cleaned = monitor.current_history(history, checks)
        self.assertEqual(set(cleaned["runs"][0]["results"]), {"warning"})
        self.assertFalse(cleaned["runs"][0]["results"]["warning"]["ok"])
        self.assertIn('class="pip down"', monitor.render_dashboard(cleaned, checks))
        self.assertIn("1 failing", monitor.render_dashboard(cleaned, checks))

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

    def test_dashboard_has_sortable_column_headers(self):
        checks = [monitor.Check("one", "One", "https://example.test", "json")]
        page = monitor.render_dashboard({"runs": []}, checks)
        for column in ("name", "status", "http", "time", "history", "error"):
            self.assertIn(f'data-sort="{column}"', page)
        self.assertEqual(page.count('scope="col" aria-sort="none"'), 6)
        self.assertIn("Number.parseInt(row.cells[2].textContent,10)", page)
        self.assertIn("Number.parseFloat(row.cells[3].textContent)", page)

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

            def __init__(self, data):
                self.data = data

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return self.data

        def open_stub(request, timeout):
            return Response(b"[]" if request.full_url.endswith("/warnings") else b'{"fine":true}')

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
