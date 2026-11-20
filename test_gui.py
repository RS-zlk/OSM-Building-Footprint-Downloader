import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import gui
from gui import DOWNLOADER, available_days, build_command, downloader_command_prefix, output_extension, snapshot_text_to_iso, snapshot_to_iso, tile_size_to_meters, validate_download_inputs


class GuiCommandTests(unittest.TestCase):
    def test_bbox_command(self):
        command = build_command("bbox", ["30.0", "120.0", "31.0", "121.0"], "", "out.geojson", "2024-01-01T00:00:00Z", "0.05", "2")
        self.assertEqual(command[:2], [sys.executable, str(DOWNLOADER)])
        self.assertIn("--bbox", command)
        self.assertIn("--date", command)

    def test_shp_requires_path(self):
        with self.assertRaisesRegex(ValueError, "SHP"):
            build_command("shp", "", "", "out.geojson", "", "", "")

    def test_structured_snapshot_is_utc(self):
        self.assertEqual(snapshot_to_iso(date(2024, 2, 29), "12", "34", "56"), "2024-02-29T12:34:56Z")

    def test_date_picker_values_are_converted_to_utc(self):
        self.assertEqual(snapshot_text_to_iso("2024-02-29", "12:34:56"), "2024-02-29T12:34:56Z")
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            snapshot_text_to_iso("2024/02/29", "12:34:56")

    def test_day_choices_follow_selected_month(self):
        self.assertEqual(available_days("2024", "02")[-1], "29")
        self.assertEqual(available_days("2025", "02")[-1], "28")

    def test_output_format_selects_expected_extension(self):
        self.assertEqual(output_extension("GeoJSON (.geojson)"), ".geojson")
        self.assertEqual(output_extension("Shapefile (.shp)"), ".shp")
        self.assertEqual(output_extension("GeoPackage (.gpkg)"), ".gpkg")

    def test_building_values_are_passed_to_command(self):
        command = build_command("bbox", ["30.0", "120.0", "31.0", "121.0"], "", "out.geojson", "", "0.05", "2", building_values="yes,house")
        self.assertIn("--building-values", command)
        self.assertEqual(command[command.index("--building-values") + 1], "yes,house")

    def test_tile_size_is_converted_to_metres_at_selected_latitude(self):
        north_south, east_west = tile_size_to_meters("0.05", 30.75)
        self.assertAlmostEqual(north_south, 5543, delta=20)
        self.assertAlmostEqual(east_west, 4786, delta=20)

    def test_invalid_bbox_is_rejected_before_starting_download(self):
        with self.assertRaisesRegex(ValueError, "南 < 北"):
            validate_download_inputs("bbox", ["31", "120", "30", "121"], "", "out.geojson", "0.05", "2")

    def test_missing_output_directory_is_accepted(self):
        validate_download_inputs("bbox", ["30", "120", "31", "121"], "", "missing_dir/out.geojson", "0.05", "2")
        with self.assertRaises(ValueError):
            validate_download_inputs("bbox", ["30", "120", "31", "121"], "", "", "0.05", "2")

    def test_invalid_advanced_options_are_rejected_before_starting_download(self):
        with self.assertRaisesRegex(ValueError, "不能小于 0"):
            validate_download_inputs("bbox", ["30", "120", "31", "121"], "", "out.geojson", "-0.05", "2")
        with self.assertRaisesRegex(ValueError, "不能小于 0"):
            validate_download_inputs("bbox", ["30", "120", "31", "121"], "", "out.geojson", "0.05", "-1")

    def test_command_prefix_uses_script_in_development_mode(self):
        """未打包时命令前缀应使用当前解释器与 download_buildings.py 脚本。"""
        self.assertEqual(downloader_command_prefix(), [sys.executable, str(DOWNLOADER)])

    def test_command_prefix_uses_bundled_cli_when_frozen(self):
        """打包运行时命令前缀应指向与 GUI 同目录的 osm-buildings-cli.exe。"""
        executable = Path("C:/app/osm-buildings-gui.exe")
        with mock.patch.object(gui.sys, "frozen", True, create=True), mock.patch.object(gui.sys, "executable", str(executable)):
            prefix = downloader_command_prefix()
        self.assertEqual(prefix, [str(executable.with_name("osm-buildings-cli.exe"))])


if __name__ == "__main__":
    unittest.main()
