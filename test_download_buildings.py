import tempfile
import time
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import shapefile
from shapely.geometry import box

import download_buildings as app


class DownloadBuildingsTests(unittest.TestCase):
    def test_split_bbox_and_limit(self):
        self.assertEqual(len(app.split_bbox([0, 0, 0.11, 0.11], 0.05, 9)), 9)
        with self.assertRaisesRegex(ValueError, "exceeding"):
            app.split_bbox([0, 0, 0.11, 0.11], 0.05, 8)

    def test_tile_progress_line_format(self):
        self.assertEqual(app.tile_progress_line(2, 5, [1.0, 2.0, 3.0, 4.0]), "[tile 2/5] south=1.0 west=2.0 north=3.0 east=4.0")

    def test_query_tile_honors_retry_after(self):
        headers = Message()
        headers["Retry-After"] = "5"
        rate_limit_error = HTTPError("https://example.invalid/api/interpreter", 429, "Too Many Requests", headers, None)
        responses = [rate_limit_error, {"elements": []}]

        def fake_fetch_json(endpoint: str, query: str, timeout: float):
            """依次返回预设响应，异常对象则直接抛出。"""
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch.object(app, "fetch_json", side_effect=fake_fetch_json), mock.patch.object(app.time, "sleep") as sleep:
            result = app.query_tile(["https://example.invalid/api/interpreter"], [0, 0, 1, 1], 10, 1, None)
        self.assertEqual(result, {"elements": []})
        self.assertIn(5, [call.args[0] for call in sleep.call_args_list])

    def test_query_tile_stops_when_deadline_exceeded(self):
        with mock.patch.object(app, "fetch_json") as fetch_json:
            with self.assertRaisesRegex(TimeoutError, "deadline"):
                app.query_tile(["https://example.invalid/api/interpreter"], [0, 0, 1, 1], 10, 0, None, time.monotonic() - 1)
        fetch_json.assert_not_called()

    def test_overpass_query_contains_date_and_bbox(self):
        """查询语句应包含快照日期子句与四个范围坐标。"""
        query = app.overpass_query([1.5, 2.5, 3.5, 4.5], 60, "2024-01-01T00:00:00Z")
        self.assertIn('[date:"2024-01-01T00:00:00Z"]', query)
        self.assertIn("(1.5,2.5,3.5,4.5)", query)

    def test_query_tile_falls_back_to_next_endpoint(self):
        """首个端点抛出 URLError 后应切换到下一个端点并返回其响应。"""
        first = "https://first.invalid/api/interpreter"
        second = "https://second.invalid/api/interpreter"
        calls = []

        def fake_fetch_json(endpoint: str, query: str, timeout: float):
            """记录请求端点，首个端点失败，第二个端点返回空结果。"""
            calls.append(endpoint)
            if endpoint == first:
                raise URLError("connection refused")
            return {"elements": []}

        with mock.patch.object(app, "fetch_json", side_effect=fake_fetch_json), mock.patch.object(app.time, "sleep"):
            result = app.query_tile([first, second], [0, 0, 1, 1], 10, 0, None)
        self.assertEqual(result, {"elements": []})
        self.assertEqual(calls, [first, second])

    def test_query_tile_reports_all_failures(self):
        """所有端点与重试均失败时应抛出汇总两个端点与尝试次数的 RuntimeError。"""
        first = "https://first.invalid/api/interpreter"
        second = "https://second.invalid/api/interpreter"
        with mock.patch.object(app, "fetch_json", side_effect=URLError("connection refused")), mock.patch.object(app.time, "sleep"):
            with self.assertRaises(RuntimeError) as raised:
                app.query_tile([first, second], [0, 0, 1, 1], 10, 1, None)
        message = str(raised.exception)
        self.assertIn(first, message)
        self.assertIn(second, message)
        self.assertIn("attempt", message)

    def test_relation_with_hole(self):
        relation = {
            "type": "relation",
            "id": 10,
            "tags": {"building": "yes"},
            "members": [
                {"role": "outer", "geometry": [{"lon": 0, "lat": 0}, {"lon": 2, "lat": 0}, {"lon": 2, "lat": 2}, {"lon": 0, "lat": 2}, {"lon": 0, "lat": 0}]},
                {"role": "inner", "geometry": [{"lon": 0.5, "lat": 0.5}, {"lon": 1.5, "lat": 0.5}, {"lon": 1.5, "lat": 1.5}, {"lon": 0.5, "lat": 1.5}, {"lon": 0.5, "lat": 0.5}]},
            ],
        }
        geometry = app.relation_geometry(relation)
        self.assertAlmostEqual(geometry.area, 3.0)

    def test_relation_stitches_open_member_ways(self):
        relation = {
            "members": [
                {"role": "outer", "geometry": [{"lon": 0, "lat": 0}, {"lon": 1, "lat": 0}]},
                {"role": "outer", "geometry": [{"lon": 1, "lat": 0}, {"lon": 1, "lat": 1}]},
                {"role": "outer", "geometry": [{"lon": 1, "lat": 1}, {"lon": 0, "lat": 1}]},
                {"role": "outer", "geometry": [{"lon": 0, "lat": 1}, {"lon": 0, "lat": 0}]},
            ]
        }
        self.assertAlmostEqual(app.relation_geometry(relation).area, 1.0)

    def test_geojson_deduplication_and_clip(self):
        element = {"type": "way", "id": 1, "tags": {"building": "yes"}, "geometry": [{"lon": 0, "lat": 0}, {"lon": 2, "lat": 0}, {"lon": 2, "lat": 2}, {"lon": 0, "lat": 2}, {"lon": 0, "lat": 0}]}
        data = app.convert_to_geojson([element, element], [0, 0, 2, 2], None, box(0, 0, 1, 1))
        self.assertEqual(len(data["features"]), 1)
        self.assertEqual(data["features"][0]["geometry"]["type"], "Polygon")

    def test_load_study_area_reprojects_shapefile(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "area.shp"
            writer = shapefile.Writer(str(path))
            writer.field("id", "N")
            writer.poly([[[0, 0], [0, 111325.14], [111319.49, 111325.14], [111319.49, 0], [0, 0]]])
            writer.record(1)
            writer.close()
            path.with_suffix(".prj").write_text(app.CRS.from_epsg(3857).to_wkt(), encoding="utf-8")
            area = app.load_study_area(path, None)
        self.assertAlmostEqual(area.bounds[2], 1.0, places=3)
        self.assertAlmostEqual(area.bounds[3], 1.0, places=3)

    def test_invalid_way_ring_is_discarded_with_reason(self):
        element = {"type": "way", "id": 1, "geometry": [{"lon": 0, "lat": 0}, {"lon": 1, "lat": 1}]}
        data = app.convert_to_geojson([element], [0, 0, 1, 1], None, None)
        self.assertEqual(data["features"], [])
        self.assertEqual(data["metadata"]["diagnostics"]["invalid_geometry"], 1)

    def test_incomplete_relation_is_discarded_with_reason(self):
        relation = {"type": "relation", "id": 3, "members": [{"role": "outer", "geometry": [{"lon": 0, "lat": 0}]}, {"role": "outer", "geometry": [{"lon": 1, "lat": 1}]}]}
        data = app.convert_to_geojson([relation], [0, 0, 1, 1], None, None)
        self.assertEqual(data["features"], [])
        self.assertEqual(data["metadata"]["diagnostics"]["incomplete_relation"], 1)

    def test_relation_member_way_is_not_emitted_twice(self):
        relation = {
            "type": "relation",
            "id": 10,
            "members": [
                {"type": "way", "ref": 5, "role": "outer", "geometry": [{"lon": 0, "lat": 0}, {"lon": 1, "lat": 0}]},
                {"type": "way", "ref": 5, "role": "outer", "geometry": [{"lon": 1, "lat": 0}, {"lon": 1, "lat": 1}]},
                {"type": "way", "ref": 5, "role": "outer", "geometry": [{"lon": 1, "lat": 1}, {"lon": 0, "lat": 1}]},
                {"type": "way", "ref": 5, "role": "outer", "geometry": [{"lon": 0, "lat": 1}, {"lon": 0, "lat": 0}]},
            ],
        }
        way = {"type": "way", "id": 5, "geometry": [{"lon": 0, "lat": 0}, {"lon": 1, "lat": 0}, {"lon": 1, "lat": 1}, {"lon": 0, "lat": 1}, {"lon": 0, "lat": 0}]}
        data = app.convert_to_geojson([relation, way], [0, 0, 1, 1], None, None)
        self.assertEqual([feature["properties"]["osm_type"] for feature in data["features"]], ["relation"])
        self.assertEqual(data["metadata"]["diagnostics"]["duplicate_member_way"], 1)

    def test_incomplete_relation_does_not_hide_valid_member_way(self):
        relation = {
            "type": "relation",
            "id": 11,
            "members": [
                {"type": "way", "ref": 5, "role": "outer", "geometry": [{"lon": 0, "lat": 0}, {"lon": 1, "lat": 0}]},
                {"type": "way", "ref": 5, "role": "outer", "geometry": [{"lon": 2, "lat": 2}, {"lon": 3, "lat": 3}]},
            ],
        }
        way = {"type": "way", "id": 5, "geometry": [{"lon": 0, "lat": 0}, {"lon": 1, "lat": 0}, {"lon": 1, "lat": 1}, {"lon": 0, "lat": 1}, {"lon": 0, "lat": 0}]}
        data = app.convert_to_geojson([relation, way], [0, 0, 1, 1], None, None)
        self.assertEqual(len(data["features"]), 1)
        self.assertEqual(data["features"][0]["properties"]["osm_id"], 5)
        self.assertEqual(data["metadata"]["diagnostics"]["incomplete_relation"], 1)
        self.assertEqual(data["metadata"]["diagnostics"]["duplicate_member_way"], 0)

    def test_node_element_is_unsupported(self):
        data = app.convert_to_geojson([{"type": "node", "id": 7}], [0, 0, 1, 1], None, None)
        self.assertEqual(data["features"], [])
        self.assertEqual(data["metadata"]["diagnostics"]["unsupported_element"], 1)

    def test_diagnostics_metadata_contains_all_keys(self):
        data = app.convert_to_geojson([], [0, 0, 1, 1], None, None)
        self.assertEqual(tuple(data["metadata"]["diagnostics"]), app.DIAGNOSTIC_KEYS)

    def test_building_values_filter_keeps_selected_types(self):
        element_yes = {"type": "way", "id": 1, "tags": {"building": "yes"}, "geometry": [{"lon": 0, "lat": 0}, {"lon": 1, "lat": 0}, {"lon": 1, "lat": 1}, {"lon": 0, "lat": 1}, {"lon": 0, "lat": 0}]}
        element_house = {"type": "way", "id": 2, "tags": {"building": "house"}, "geometry": [{"lon": 2, "lat": 0}, {"lon": 3, "lat": 0}, {"lon": 3, "lat": 1}, {"lon": 2, "lat": 1}, {"lon": 2, "lat": 0}]}
        data = app.convert_to_geojson([element_yes, element_house], [0, 0, 3, 3], None, None, building_values={"yes"})
        self.assertEqual(len(data["features"]), 1)
        self.assertEqual(data["features"][0]["properties"]["osm_id"], 1)
        self.assertEqual(data["metadata"]["diagnostics"]["filtered_elements"], 1)

    @unittest.skipUnless(app.fiona is not None, "需要安装 fiona 才能测试 GeoPackage 输出")
    def test_write_geopackage_preserves_features_and_metadata(self):
        diagnostics = {"invalid_geometry": 1, "incomplete_relation": 0, "non_polygon": 1, "duplicate_member_way": 0, "unsupported_element": 2, "filtered_elements": 0}
        data = {
            "metadata": {"crs": "EPSG:4326", "diagnostics": diagnostics},
            "features": [
                {"type": "Feature", "properties": {"osm_id": 1, "building:levels": "2", "名称": "测试建筑"}, "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}},
                {"type": "Feature", "properties": {"osm_id": 2, "building:levels": "3"}, "geometry": {"type": "MultiPolygon", "coordinates": [[[[2, 0], [3, 0], [3, 1], [2, 1], [2, 0]]], [[[4, 0], [5, 0], [5, 1], [4, 1], [4, 0]]]]}},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "buildings.gpkg"
            self.assertEqual(app.write_output(path, data), "GeoPackage")
            with app.fiona.open(path, layer="buildings") as source:
                schema = source.schema
                properties = [dict(feature["properties"]) for feature in source]
            self.assertEqual(len(properties), 2)
            self.assertEqual(properties[0]["osm_id"], 1)
            self.assertEqual(properties[0]["building:levels"], "2")
            self.assertEqual(properties[0]["名称"], "测试建筑")
            self.assertEqual(properties[1]["building:levels"], "3")
            self.assertEqual(schema["geometry"], "MultiPolygon")
            self.assertIn("building:levels", schema["properties"])
            self.assertIn("名称", schema["properties"])
            metadata = app.json.loads(path.with_suffix(".metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["diagnostics"], diagnostics)

    def test_cache_roundtrip_and_key_changes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_dir = Path(temporary_directory)
            key = app.cache_key([0, 0, 1, 1], None)
            self.assertIsNone(app.read_cached_response(cache_dir, key))
            app.write_cached_response(cache_dir, key, {"elements": [{"id": 1}]})
            self.assertEqual(app.read_cached_response(cache_dir, key), {"elements": [{"id": 1}]})
            self.assertIsNone(app.read_cached_response(cache_dir, app.cache_key([0, 0, 2, 2], None)))
            self.assertNotEqual(key, app.cache_key([0, 0, 1, 1], "2024-01-01T00:00:00Z"))

    def test_collect_tile_responses_uses_cache(self):
        tiles = [[0, 0, 1, 1], [1, 1, 2, 2], [2, 2, 3, 3]]
        cached = {"elements": [{"cached": True}]}
        calls = []

        def fake_query_tile(endpoints, bbox, timeout, retries, snapshot_date, deadline=None):
            """记录被请求的分块并返回带范围标记的响应。"""
            calls.append(list(bbox))
            return {"elements": [{"bbox": list(bbox)}]}

        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_dir = Path(temporary_directory)
            app.write_cached_response(cache_dir, app.cache_key(tiles[1], None), cached)
            with mock.patch.object(app, "query_tile", side_effect=fake_query_tile):
                responses = app.collect_tile_responses(("https://example.invalid/api/interpreter",), tiles, 10, 0, None, None, 1, cache_dir, False, False)
        self.assertEqual(len(calls), 2)
        self.assertEqual(responses, [{"elements": [{"bbox": tiles[0]}]}, cached, {"elements": [{"bbox": tiles[2]}]}])

    def test_collect_tile_responses_parallel_preserves_order(self):
        tiles = [[0, 0, 1, 1], [1, 1, 2, 2], [2, 2, 3, 3], [3, 3, 4, 4]]

        def fake_query_tile(endpoints, bbox, timeout, retries, snapshot_date, deadline=None):
            """按与输入相反的顺序制造完成延迟，并返回带范围标记的响应。"""
            time.sleep(0.05 * (len(tiles) - tiles.index(bbox)))
            return {"elements": [{"bbox": list(bbox)}]}

        with mock.patch.object(app, "query_tile", side_effect=fake_query_tile):
            responses = app.collect_tile_responses(("https://example.invalid/api/interpreter",), tiles, 10, 0, None, None, 4, None, False, False)
        self.assertEqual([response["elements"][0]["bbox"] for response in responses], tiles)

    def test_write_shapefile_preserves_features_and_sidecars(self):
        diagnostics = {"invalid_geometry": 2, "incomplete_relation": 0, "non_polygon": 1, "duplicate_member_way": 0, "unsupported_element": 3, "filtered_elements": 0}
        data = {
            "metadata": {"crs": "EPSG:4326", "diagnostics": diagnostics},
            "features": [
                {"type": "Feature", "properties": {"osm_id": 1, "building:levels": "2", "名称": "测试建筑"}, "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}},
                {"type": "Feature", "properties": {"osm_id": 2, "building:levels": "3"}, "geometry": {"type": "MultiPolygon", "coordinates": [[[[2, 0], [3, 0], [3, 1], [2, 1], [2, 0]]], [[[4, 0], [5, 0], [5, 1], [4, 1], [4, 0]]]]}},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "buildings.shp"
            self.assertEqual(app.write_output(path, data), "Shapefile")
            reader = shapefile.Reader(str(path), encoding="utf-8")
            try:
                self.assertEqual(len(reader), 2)
            finally:
                reader.close()
            self.assertTrue(path.with_suffix(".prj").exists())
            self.assertTrue(path.with_suffix(".cpg").exists())
            mapping = app.json.loads(path.with_suffix(".fields.json").read_text(encoding="utf-8"))
            self.assertIn("building:levels", mapping.values())
            self.assertIn("名称", mapping.values())
            metadata = app.json.loads(path.with_suffix(".metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["diagnostics"], diagnostics)


if __name__ == "__main__":
    unittest.main()
