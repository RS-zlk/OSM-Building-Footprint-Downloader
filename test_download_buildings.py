import tempfile
import unittest
from pathlib import Path

import shapefile
from shapely.geometry import box

import download_buildings as app


class DownloadBuildingsTests(unittest.TestCase):
    def test_split_bbox_and_limit(self):
        self.assertEqual(len(app.split_bbox([0, 0, 0.11, 0.11], 0.05, 9)), 9)
        with self.assertRaisesRegex(ValueError, "exceeding"):
            app.split_bbox([0, 0, 0.11, 0.11], 0.05, 8)

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

    def test_write_shapefile_preserves_features_and_sidecars(self):
        data = {
            "metadata": {"crs": "EPSG:4326"},
            "features": [
                {"type": "Feature", "properties": {"osm_id": 1, "building:levels": "2", "名称": "测试建筑"}, "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}},
                {"type": "Feature", "properties": {"osm_id": 2, "building:levels": "3"}, "geometry": {"type": "MultiPolygon", "coordinates": [[[[2, 0], [3, 0], [3, 1], [2, 1], [2, 0]]], [[[4, 0], [5, 0], [5, 1], [4, 1], [4, 0]]]]}},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "buildings.shp"
            self.assertEqual(app.write_output(path, data), "Shapefile")
            reader = shapefile.Reader(str(path), encoding="utf-8")
            self.assertEqual(len(reader), 2)
            self.assertTrue(path.with_suffix(".prj").exists())
            self.assertTrue(path.with_suffix(".cpg").exists())
            mapping = app.json.loads(path.with_suffix(".fields.json").read_text(encoding="utf-8"))
            self.assertIn("building:levels", mapping.values())
            self.assertIn("名称", mapping.values())


if __name__ == "__main__":
    unittest.main()
