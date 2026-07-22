import sys
import unittest
from datetime import date

from gui import DOWNLOADER, available_days, build_command, output_extension, snapshot_to_iso


class GuiCommandTests(unittest.TestCase):
    def test_bbox_command(self):
        command = build_command("bbox", ["30.0", "120.0", "31.0", "121.0"], "", "data/out.geojson", "2024-01-01T00:00:00Z", "0.05", "2")
        self.assertEqual(command[:2], [sys.executable, str(DOWNLOADER)])
        self.assertIn("--bbox", command)
        self.assertIn("--date", command)

    def test_shp_requires_path(self):
        with self.assertRaisesRegex(ValueError, "SHP"):
            build_command("shp", "", "", "data/out.geojson", "", "", "")

    def test_structured_snapshot_is_utc(self):
        self.assertEqual(snapshot_to_iso(date(2024, 2, 29), "12", "34", "56"), "2024-02-29T12:34:56Z")

    def test_day_choices_follow_selected_month(self):
        self.assertEqual(available_days("2024", "02")[-1], "29")
        self.assertEqual(available_days("2025", "02")[-1], "28")

    def test_output_format_selects_expected_extension(self):
        self.assertEqual(output_extension("GeoJSON (.geojson)"), ".geojson")
        self.assertEqual(output_extension("Shapefile (.shp)"), ".shp")


if __name__ == "__main__":
    unittest.main()
