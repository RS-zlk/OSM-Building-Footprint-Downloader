#!/usr/bin/env python3
"""Download current or historical OSM building footprints as GeoJSON or SHP."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Prefer the active Conda environment's projection database over an inherited
# base-environment path. This must happen before importing pyproj.
_PROJ_DATA = Path(sys.prefix) / "share" / "proj"
if _PROJ_DATA.is_dir():
    os.environ["PROJ_DATA"] = str(_PROJ_DATA)
    os.environ["PROJ_LIB"] = str(_PROJ_DATA)

import shapefile
from pyproj import CRS, Transformer, datadir
from shapely.geometry import LineString, Polygon, mapping, shape
from shapely.ops import polygonize, transform, unary_union

DEFAULT_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = "osm-building-downloader/2.0 (local data export)"
# 丢弃与过滤诊断项的固定顺序，保证 CLI 摘要与元数据输出稳定
DIAGNOSTIC_KEYS = ("invalid_geometry", "incomplete_relation", "non_polygon", "duplicate_member_way", "unsupported_element", "filtered_elements")

if _PROJ_DATA.is_dir():
    datadir.set_data_dir(str(_PROJ_DATA))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download OSM building footprints within a WGS84 extent.")
    extent = parser.add_mutually_exclusive_group(required=True)
    extent.add_argument("--bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"), help="WGS84 extent, ordered south west north east.")
    extent.add_argument("--shp", type=Path, help="Study-area ESRI Shapefile. Its geometry is used to clip results.")
    parser.add_argument("--output", type=Path, required=True, help="Destination .geojson, .json, or .shp path.")
    parser.add_argument("--endpoint", action="append", help="Overpass API URL. Repeat to define fallback endpoints.")
    parser.add_argument("--date", help="Optional historical OSM snapshot in ISO 8601 UTC, e.g. 2024-01-01T00:00:00Z.")
    parser.add_argument("--timeout", type=int, default=120, help="Overpass and HTTP timeout in seconds (default: 120).")
    parser.add_argument("--retries", type=int, default=2, help="Retries per endpoint after a transient failure (default: 2).")
    parser.add_argument("--tile-size", type=float, default=0.05, help="Maximum tile width/height in degrees; use 0 to disable splitting (default: 0.05).")
    parser.add_argument("--max-tiles", type=int, default=2_000, help="Safety limit for generated query tiles (default: 2000).")
    parser.add_argument("--shp-crs", help="Override the input SHP CRS, e.g. EPSG:3857. Otherwise its .prj file is required.")
    parser.add_argument("--save-raw", type=Path, help="Optional path to save raw Overpass responses as JSON.")
    return parser.parse_args()


def validate_bbox(bbox: list[float]) -> None:
    south, west, north, east = bbox
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise ValueError("Invalid bbox. Expected -90 <= south < north <= 90 and -180 <= west < east <= 180.")


def parse_snapshot_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid --date. Use ISO 8601 UTC, e.g. 2024-01-01T00:00:00Z.") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("Invalid --date. Specify UTC with Z or +00:00.")
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_study_area(path: Path, crs_override: str | None) -> Any:
    """Load all polygonal SHP features, transform them to WGS84, and dissolve them."""
    try:
        reader = shapefile.Reader(str(path))
    except (OSError, shapefile.ShapefileException) as error:
        raise ValueError(f"Could not read shapefile {path}: {error}") from error
    try:
        geometries = [shape(record.shape.__geo_interface__) for record in reader.shapeRecords() if record.shape.shapeType != shapefile.NULL]
    finally:
        reader.close()
    if not geometries:
        raise ValueError(f"Shapefile {path} contains no geometries.")
    if not all(geometry.geom_type in {"Polygon", "MultiPolygon"} for geometry in geometries):
        raise ValueError("--shp must contain Polygon or MultiPolygon features.")
    if crs_override:
        source_crs = CRS.from_user_input(crs_override)
    else:
        prj_path = path.with_suffix(".prj")
        if not prj_path.exists():
            raise ValueError(f"Missing {prj_path.name}. Provide --shp-crs to specify the shapefile CRS.")
        source_crs = CRS.from_wkt(prj_path.read_text(encoding="utf-8"))
    area = unary_union(geometries)
    if source_crs != CRS.from_epsg(4326):
        transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
        area = transform(transformer.transform, area)
    if area.is_empty or not area.is_valid:
        raise ValueError("Study-area geometry is empty or invalid.")
    west, south, east, north = area.bounds
    validate_bbox([south, west, north, east])
    return area


def split_bbox(bbox: list[float], tile_size: float, max_tiles: int) -> list[list[float]]:
    validate_bbox(bbox)
    if tile_size <= 0:
        return [bbox]
    if max_tiles < 1:
        raise ValueError("--max-tiles must be at least 1.")
    south, west, north, east = bbox
    rows = math.ceil((north - south) / tile_size)
    columns = math.ceil((east - west) / tile_size)
    if rows * columns > max_tiles:
        raise ValueError(f"Query would create {rows * columns} tiles, exceeding --max-tiles={max_tiles}. Increase --tile-size or --max-tiles.")
    return [[lat, lon, min(lat + tile_size, north), min(lon + tile_size, east)] for lat in (south + row * tile_size for row in range(rows)) for lon in (west + column * tile_size for column in range(columns))]


def overpass_query(bbox: list[float], timeout: int, snapshot_date: str | None) -> str:
    south, west, north, east = bbox
    date_clause = f'[date:"{snapshot_date}"]' if snapshot_date else ""
    return f'[out:json][timeout:{timeout}]{date_clause};(way["building"]({south},{west},{north},{east});relation["building"]["type"="multipolygon"]({south},{west},{north},{east}););out geom;'


def fetch_json(endpoint: str, query: str, timeout: int) -> dict[str, Any]:
    request = Request(endpoint, data=urlencode({"data": query}).encode("utf-8"), headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout + 15) as response:
        return json.load(response)


def query_tile(endpoints: Iterable[str], bbox: list[float], timeout: int, retries: int, snapshot_date: str | None) -> dict[str, Any]:
    query = overpass_query(bbox, timeout, snapshot_date)
    errors: list[str] = []
    for endpoint in endpoints:
        for attempt in range(retries + 1):
            try:
                return fetch_json(endpoint, query, timeout)
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
                errors.append(f"{endpoint} (attempt {attempt + 1}): {error}")
                if attempt < retries:
                    time.sleep(min(2**attempt, 4))
    raise RuntimeError("All Overpass requests failed:\n" + "\n".join(errors))


def polygon_from_ring(coordinates: list[dict[str, float]]) -> Polygon | None:
    points = [(point["lon"], point["lat"]) for point in coordinates]
    if len(points) < 3:
        return None
    polygon = Polygon(points)
    return polygon if polygon.is_valid and not polygon.is_empty else None


def relation_geometry(element: dict[str, Any]) -> Any | None:
    outer_lines, inner_lines = [], []
    for member in element.get("members", []):
        points = [(point["lon"], point["lat"]) for point in member.get("geometry", [])]
        if len(points) < 2:
            continue
        (inner_lines if member.get("role") == "inner" else outer_lines).append(LineString(points))
    outer_polygons = list(polygonize(outer_lines))
    if not outer_polygons:
        return None
    geometry = unary_union(outer_polygons)
    inner_polygons = list(polygonize(inner_lines))
    if inner_polygons:
        geometry = geometry.difference(unary_union(inner_polygons))
    return geometry if not geometry.is_empty and geometry.is_valid else None


def element_geometry_with_reason(element: dict[str, Any]) -> tuple[Any | None, str | None]:
    """返回要素几何及其失败原因；成功时原因为 None。"""
    if element.get("type") == "way":
        geometry = polygon_from_ring(element.get("geometry", []))
        return (geometry, None) if geometry is not None else (None, "invalid_geometry")
    if element.get("type") == "relation":
        geometry = relation_geometry(element)
        return (geometry, None) if geometry is not None else (None, "incomplete_relation")
    return None, "unsupported_element"


def element_geometry(element: dict[str, Any]) -> Any | None:
    """只返回要素几何，保留原有调用方式。"""
    return element_geometry_with_reason(element)[0]


def convert_to_geojson(source: Iterable[dict[str, Any]], bbox: list[float], snapshot_date: str | None, clip_geometry: Any | None) -> dict[str, Any]:
    """把 Overpass 元素转换为 GeoJSON，并在元数据中记录丢弃诊断。"""
    elements = list(source)
    # 只从能生成有效几何的 relation 收集成员 way，避免残缺 relation 导致独立 way 被误判为重复
    member_way_refs = {
        member["ref"]
        for element in elements
        if element.get("type") == "relation" and relation_geometry(element) is not None
        for member in element.get("members", [])
        if member.get("type") == "way" and isinstance(member.get("ref"), int)
    }
    diagnostics = {key: 0 for key in DIAGNOSTIC_KEYS}
    features = []
    seen: set[tuple[str, int]] = set()
    for element in elements:
        osm_type, osm_id = element.get("type"), element.get("id")
        if osm_type not in {"way", "relation"} or not isinstance(osm_id, int):
            diagnostics["unsupported_element"] += 1
            continue
        key = (osm_type, osm_id)
        if key in seen:
            continue
        seen.add(key)
        if osm_type == "way" and osm_id in member_way_refs:
            diagnostics["duplicate_member_way"] += 1
            continue
        geometry, reason = element_geometry_with_reason(element)
        if geometry is None:
            if reason is not None:
                diagnostics[reason] += 1
            continue
        if clip_geometry is not None:
            geometry = geometry.intersection(clip_geometry)
            if geometry.is_empty:
                continue
        if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            diagnostics["non_polygon"] += 1
            continue
        properties = {"osm_type": osm_type, "osm_id": osm_id, **element.get("tags", {})}
        features.append({"type": "Feature", "properties": properties, "geometry": mapping(geometry)})
    note = "Contains OSM building ways and multipolygon relations."
    if clip_geometry is not None:
        note += " Features are clipped to the supplied study-area shapefile."
    return {"type": "FeatureCollection", "metadata": {"source": "OpenStreetMap contributors, via Overpass API", "licence": "ODbL 1.0", "crs": "EPSG:4326 (WGS 84)", "query_bbox_wgs84_south_west_north_east": bbox, "requested_osm_snapshot_utc": snapshot_date, "downloaded_at_utc": datetime.now(timezone.utc).isoformat(), "note": note, "diagnostics": diagnostics}, "features": features}


def shapefile_field_name(name: str, used: set[str]) -> str:
    """Return a unique DBF-compatible field name (ASCII and at most 10 chars)."""
    normalized = "".join(character if character.isascii() and (character.isalnum() or character == "_") else "_" for character in name).upper().strip("_") or "FIELD"
    normalized = normalized[:10]
    candidate = normalized
    suffix = 1
    while candidate in used:
        suffix_text = str(suffix)
        candidate = f"{normalized[:10 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used.add(candidate)
    return candidate


def shapefile_fields(features: list[dict[str, Any]]) -> list[tuple[str, str, str, int, int]]:
    """Create DBF field definitions while preserving all feature properties."""
    property_names = sorted({name for feature in features for name in feature["properties"]})
    used: set[str] = set()
    definitions = []
    for name in property_names:
        values = [feature["properties"].get(name) for feature in features if feature["properties"].get(name) is not None]
        field_name = shapefile_field_name(name, used)
        if values and all(isinstance(value, bool) for value in values):
            definitions.append((name, field_name, "L", 1, 0))
        elif values and all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            definitions.append((name, field_name, "N", 18, 0))
        elif values and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
            definitions.append((name, field_name, "F", 18, 6))
        else:
            definitions.append((name, field_name, "C", 254, 0))
    return definitions


def write_geojson(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_shapefile(path: Path, data: dict[str, Any]) -> None:
    """Write polygon features plus CRS, metadata and DBF field mapping sidecars."""
    path.parent.mkdir(parents=True, exist_ok=True)
    features = data["features"]
    fields = shapefile_fields(features)
    with shapefile.Writer(str(path), shapeType=shapefile.POLYGON, encoding="utf-8") as writer:
        for _original, field_name, field_type, size, decimal in fields:
            writer.field(field_name, field_type, size=size, decimal=decimal)
        for feature in features:
            writer.shape(feature["geometry"])
            record = []
            for original, _field_name, field_type, _size, _decimal in fields:
                value = feature["properties"].get(original)
                record.append(str(value) if value is not None and field_type == "C" else value)
            writer.record(*record)
    path.with_suffix(".prj").write_text(CRS.from_epsg(4326).to_wkt(), encoding="utf-8")
    path.with_suffix(".cpg").write_text("UTF-8\n", encoding="ascii")
    path.with_suffix(".metadata.json").write_text(json.dumps(data["metadata"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    field_mapping = {field_name: original for original, field_name, _type, _size, _decimal in fields}
    path.with_suffix(".fields.json").write_text(json.dumps(field_mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_output(path: Path, data: dict[str, Any]) -> str:
    suffix = path.suffix.lower()
    if suffix in {".geojson", ".json"}:
        write_geojson(path, data)
        return "GeoJSON"
    if suffix == ".shp":
        write_shapefile(path, data)
        return "Shapefile"
    raise ValueError("--output must end in .geojson, .json, or .shp.")


def main() -> int:
    args = parse_args()
    try:
        clip_geometry = load_study_area(args.shp, args.shp_crs) if args.shp else None
        if clip_geometry is not None:
            west, south, east, north = clip_geometry.bounds
            bbox = [south, west, north, east]
        else:
            bbox = args.bbox
        validate_bbox(bbox)
        if args.retries < 0:
            raise ValueError("--retries must be zero or greater.")
        snapshot_date = parse_snapshot_date(args.date)
        tiles = split_bbox(bbox, args.tile_size, args.max_tiles)
        endpoints = tuple(args.endpoint) if args.endpoint else DEFAULT_ENDPOINTS
        raw_responses = [query_tile(endpoints, tile, args.timeout, args.retries, snapshot_date) for tile in tiles]
    except (ValueError, RuntimeError, HTTPError, URLError, TimeoutError) as error:
        print(f"Download failed: {error}", file=sys.stderr)
        return 1
    elements = [element for response in raw_responses for element in response.get("elements", [])]
    if args.save_raw:
        args.save_raw.parent.mkdir(parents=True, exist_ok=True)
        args.save_raw.write_text(json.dumps({"responses": raw_responses}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    geojson = convert_to_geojson(elements, bbox, snapshot_date, clip_geometry)
    geojson["metadata"]["query_tiles"] = len(tiles)
    try:
        output_format = write_output(args.output, geojson)
    except (OSError, ValueError, shapefile.ShapefileException) as error:
        print(f"Output failed: {error}", file=sys.stderr)
        return 1
    diagnostics = geojson["metadata"]["diagnostics"]
    if any(diagnostics.values()):
        print("丢弃/过滤统计：" + ", ".join(f"{key}={value}" for key, value in diagnostics.items() if value))
    print(f"Saved {len(geojson['features'])} building footprints as {output_format} from {len(tiles)} tile(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
