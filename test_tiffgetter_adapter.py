import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tiffgetter_adapter as adapter

DIAGNOSTICS = {
    "invalid_geometry": 0,
    "incomplete_relation": 0,
    "non_polygon": 0,
    "duplicate_member_way": 0,
    "unsupported_element": 0,
    "filtered_elements": 0,
}


def sample_data() -> dict:
    """构造包含一个 Polygon 要素的模拟下载结果。"""
    return {
        "type": "FeatureCollection",
        "metadata": {
            "diagnostics": dict(DIAGNOSTICS),
            "query_bbox_wgs84_south_west_north_east": [30.0, 120.0, 30.2, 120.2],
        },
        "features": [
            {
                "type": "Feature",
                "properties": {"osm_id": 1},
                "geometry": {"type": "Polygon", "coordinates": [[[120.0, 30.0], [120.1, 30.0], [120.1, 30.1], [120.0, 30.1], [120.0, 30.0]]]},
            }
        ],
    }


def fake_fetch_and_convert(**kwargs):
    """模拟下载：调用一次进度回调并返回固定数据、分块与原始响应。"""
    on_progress = kwargs.get("on_progress")
    if on_progress is not None:
        on_progress("[tile 1/1] south=30.0 west=120.0 north=30.2 east=120.2")
    return sample_data(), [[30.0, 120.0, 30.2, 120.2]], [{"elements": []}]


def valid_request(root: str, request_id: str = "demo-1") -> dict:
    """构造一份合法请求，输出根目录由调用方指定。"""
    return {
        "schema_version": "1.0",
        "request_id": request_id,
        "aoi": {"mode": "bbox", "bbox": [30.0, 120.0, 30.2, 120.2]},
        "output": {"root": root, "formats": ["geojson"]},
    }


class TiffGetterAdapterTests(unittest.TestCase):
    def test_run_request_success_writes_assets_and_events(self):
        """成功请求应写出数据、元数据，并产生 log、progress 与 run_summary 事件。"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            config = adapter.parse_request(valid_request(str(temporary / "out")))
            events_path = temporary / "events.ndjson"
            events = adapter.EventWriter(events_path)
            with mock.patch.object(adapter.downloader, "fetch_and_convert", side_effect=fake_fetch_and_convert):
                result = adapter.run_request(config, events, "2026-01-01T00:00:00+00:00")
            events.close()
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["status"], "success")
            item = result["items"][0]
            self.assertTrue(Path(item["assets"]["geojson"]).exists())
            self.assertTrue(Path(item["assets"]["metadata"]).exists())
            self.assertEqual(item["feature_count"], 1)
            self.assertEqual(item["diagnostics"], DIAGNOSTICS)
            self.assertEqual(item["bounds"], {"min_lon": 120.0, "min_lat": 30.0, "max_lon": 120.2, "max_lat": 30.2})
            records = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(record["sequence"] == index for index, record in enumerate(records, start=1)))
            event_types = [record["type"] for record in records]
            self.assertIn("log", event_types)
            self.assertIn("progress", event_types)
            self.assertIn("run_summary", event_types)

    def test_parse_request_defaults_formats_to_geojson(self):
        """省略 output.formats 时应默认只输出 GeoJSON。"""
        payload = {"schema_version": "1.0", "request_id": "demo-1", "aoi": {"mode": "bbox", "bbox": [30.0, 120.0, 30.2, 120.2]}, "output": {"root": "data/output/buildings"}}
        config = adapter.parse_request(payload)
        self.assertEqual(config["formats"], ["geojson"])

    def test_main_writes_geojson_when_formats_omitted(self):
        """省略 output.formats 的命令行请求应返回 0 并写出 buildings.geojson。"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            payload = {"schema_version": "1.0", "request_id": "default-1", "aoi": {"mode": "bbox", "bbox": [30.0, 120.0, 30.2, 120.2]}, "output": {"root": str(temporary / "out")}}
            request_path = temporary / "request.json"
            request_path.write_text(json.dumps(payload), encoding="utf-8")
            result_path = temporary / "result.json"
            with mock.patch.object(adapter.downloader, "fetch_and_convert", side_effect=fake_fetch_and_convert):
                exit_code = adapter.main(["--request", str(request_path), "--result", str(result_path)])
            self.assertEqual(exit_code, 0)
            self.assertTrue((temporary / "out" / "buildings.geojson").exists())
            result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "success")

    def test_main_reports_exit_code_2_when_aoi_missing(self):
        """缺少 aoi 的请求应返回退出码 2，并写出失败结果。"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            payload = {"schema_version": "1.0", "request_id": "bad-1", "output": {"root": str(temporary / "out"), "formats": ["geojson"]}}
            request_path = temporary / "request.json"
            request_path.write_text(json.dumps(payload), encoding="utf-8")
            result_path = temporary / "result.json"
            exit_code = adapter.main(["--request", str(request_path), "--result", str(result_path)])
            result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["items"], [])
        self.assertIn("aoi", result["detail"])

    def test_main_reports_exit_code_4_when_output_root_is_a_file(self):
        """输出根目录指向已存在文件时应返回退出码 4，并写出失败结果。"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            blocker = temporary / "blocker"
            blocker.write_text("x", encoding="utf-8")
            request_path = temporary / "request.json"
            request_path.write_text(json.dumps(valid_request(str(blocker), "fail-1")), encoding="utf-8")
            result_path = temporary / "result.json"
            with mock.patch.object(adapter.downloader, "fetch_and_convert", side_effect=fake_fetch_and_convert):
                exit_code = adapter.main(["--request", str(request_path), "--result", str(result_path)])
            result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 4)

    def test_main_success_via_cli_files(self):
        """命令行入口应写出可解析的结果文件并返回 0。"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            request_path = temporary / "request.json"
            request_path.write_text(json.dumps(valid_request(str(temporary / "out"), "cli-1")), encoding="utf-8")
            result_path = temporary / "result.json"
            with mock.patch.object(adapter.downloader, "fetch_and_convert", side_effect=fake_fetch_and_convert):
                exit_code = adapter.main(["--request", str(request_path), "--result", str(result_path)])
            self.assertEqual(exit_code, 0)
            self.assertTrue(result_path.exists())
            result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(result["request_id"], "cli-1")
        self.assertEqual(result["status"], "success")


if __name__ == "__main__":
    unittest.main()
