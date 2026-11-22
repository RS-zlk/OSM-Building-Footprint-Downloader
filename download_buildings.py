#!/usr/bin/env python3
"""Download current or historical OSM building footprints as GeoJSON or SHP."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.client
import importlib.util
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Prefer the active Conda environment's projection database over an inherited
# base-environment path. This must happen before importing pyproj.
_PROJ_DATA = Path(sys.prefix) / "share" / "proj"
if _PROJ_DATA.is_dir():
    os.environ["PROJ_DATA"] = str(_PROJ_DATA)
    os.environ["PROJ_LIB"] = str(_PROJ_DATA)

import shapefile  # noqa: E402
from pyproj import CRS, Transformer, datadir  # noqa: E402
from shapely.geometry import LineString, MultiPolygon, Polygon, mapping, shape  # noqa: E402
from shapely.ops import polygonize, transform, unary_union  # noqa: E402

try:
    # Prefer Fiona's bundled database for pip wheels over inherited system paths.
    fiona_spec = importlib.util.find_spec("fiona")
    if not _PROJ_DATA.is_dir() and fiona_spec and fiona_spec.origin:
        fiona_proj_data = Path(fiona_spec.origin).parent / "proj_data"
        if (fiona_proj_data / "proj.db").is_file():
            os.environ["PROJ_DATA"] = str(fiona_proj_data)
            os.environ["PROJ_LIB"] = str(fiona_proj_data)
    import fiona
except ImportError:
    fiona = None  # type: ignore[assignment]

DEFAULT_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = "osm-building-downloader/2.1 (local data export)"
# 丢弃与过滤诊断项的固定顺序，保证 CLI 摘要与元数据输出稳定
DIAGNOSTIC_KEYS = ("invalid_geometry", "incomplete_relation", "non_polygon", "duplicate_member_way", "unsupported_element", "filtered_elements")
CURRENT_CACHE_MAX_AGE = 24 * 60 * 60

if _PROJ_DATA.is_dir():
    datadir.set_data_dir(str(_PROJ_DATA))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download OSM building footprints within a WGS84 extent.")
    extent = parser.add_mutually_exclusive_group(required=True)
    extent.add_argument("--bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"), help="WGS84 extent, ordered south west north east.")
    extent.add_argument("--shp", type=Path, help="Study-area ESRI Shapefile. Its geometry is used to clip results.")
    parser.add_argument("--output", type=Path, required=True, help="Destination .geojson, .json, .shp, or .gpkg path.")
    parser.add_argument("--building-values", help="逗号分隔的 building 取值；留空表示全部。")
    parser.add_argument("--cache-dir", type=Path, help="分块响应缓存目录，用于断点续跑。")
    parser.add_argument("--refresh-cache", action="store_true", help="忽略已有缓存并重新请求每个分块。")
    parser.add_argument("--jobs", type=int, default=1, help="并行分块查询数，取值 1-8（默认: 1）。")
    parser.add_argument("--endpoint", action="append", help="Overpass API URL. Repeat to define fallback endpoints.")
    parser.add_argument("--date", help="Optional historical OSM snapshot in ISO 8601 UTC, e.g. 2024-01-01T00:00:00Z.")
    parser.add_argument("--timeout", type=int, default=120, help="Overpass and HTTP timeout in seconds (default: 120).")
    parser.add_argument("--retries", type=int, default=2, help="Retries per endpoint after a transient failure (default: 2).")
    parser.add_argument("--deadline", type=int, default=0, help="整次运行的总时限（秒），0 表示不限制。")
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


def tile_progress_line(index: int, total: int, bbox: list[float]) -> str:
    """返回单个分块查询的进度行，包含分块序号与四至范围。"""
    return f"[tile {index}/{total}] south={bbox[0]} west={bbox[1]} north={bbox[2]} east={bbox[3]}"


def overpass_query(bbox: list[float], timeout: int, snapshot_date: str | None) -> str:
    south, west, north, east = bbox
    date_clause = f'[date:"{snapshot_date}"]' if snapshot_date else ""
    return f'[out:json][timeout:{timeout}]{date_clause};(way["building"]({south},{west},{north},{east});relation["building"]["type"="multipolygon"]({south},{west},{north},{east}););out geom;'


def fetch_json(endpoint: str, query: str, timeout: float) -> dict[str, Any]:
    """请求 Overpass 接口并解析 JSON，超时值由调用方直接决定。"""
    request = Request(endpoint, data=urlencode({"data": query}).encode("utf-8"), headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def retry_after_seconds(value: str | None) -> float | None:
    """解析 Retry-After 响应头，支持纯数字秒与 HTTP-date，无法解析时返回 None。"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(text)
    except ValueError:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return max((target - datetime.now(timezone.utc)).total_seconds(), 0.0)


def query_tile(endpoints: Iterable[str], bbox: list[float], timeout: int, retries: int, snapshot_date: str | None, deadline: float | None = None) -> dict[str, Any]:
    """查询单个分块，按端点与重试次数请求，并遵守总时限与 Retry-After。"""
    query = overpass_query(bbox, timeout, snapshot_date)
    errors: list[str] = []
    for endpoint in endpoints:
        for attempt in range(retries + 1):
            request_timeout = float(timeout + 15)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("已超出总时限（--deadline）")
                request_timeout = max(min(timeout + 15, remaining), 1)
            try:
                return fetch_json(endpoint, query, request_timeout)
            except HTTPError as error:
                errors.append(f"{endpoint} (attempt {attempt + 1}): {error}")
                if attempt < retries:
                    wait = retry_after_seconds(error.headers.get("Retry-After") if error.headers is not None else None)
                    if wait is None:
                        wait = min(2**attempt, 4)
                    if deadline is not None:
                        wait = max(min(wait, deadline - time.monotonic()), 0)
                    time.sleep(wait)
            except (URLError, TimeoutError, json.JSONDecodeError, http.client.RemoteDisconnected) as error:
                errors.append(f"{endpoint} (attempt {attempt + 1}): {error}")
                if attempt < retries:
                    time.sleep(min(2**attempt, 4))
    raise RuntimeError("All Overpass requests failed:\n" + "\n".join(errors))


def cache_key(bbox: list[float], snapshot_date: str | None) -> str:
    """根据范围和快照时刻生成稳定的缓存键。"""
    payload = json.dumps({"bbox": list(bbox), "snapshot_date": snapshot_date}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_cached_response(cache_dir: Path, key: str, max_age: float | None = None) -> dict[str, Any] | None:
    """读取分块缓存响应，文件不存在时返回 None。"""
    path = cache_dir / f"{key}.json"
    if not path.exists():
        return None
    if max_age is not None and time.time() - path.stat().st_mtime >= max_age:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_cached_response(cache_dir: Path, key: str, response: dict[str, Any]) -> None:
    """先写临时文件再原子替换，避免中断留下半截缓存。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = cache_dir / f"{key}.json.tmp"
    temporary.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, cache_dir / f"{key}.json")


def collect_tile_responses(endpoints: Iterable[str], tiles: list[list[float]], timeout: int, retries: int, snapshot_date: str | None, deadline: float | None, jobs: int, cache_dir: Path | None, refresh_cache: bool, show_progress: bool, on_progress: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """按分块收集 Overpass 响应，支持缓存命中与并行执行。"""
    def process(item: tuple[int, list[float]]) -> dict[str, Any]:
        """处理单个分块：输出进度、读取缓存、必要时请求并写回缓存。"""
        index, tile = item
        if on_progress is not None:
            on_progress(tile_progress_line(index, len(tiles), tile))
        elif show_progress:
            print(tile_progress_line(index, len(tiles), tile), flush=True)
        if cache_dir is not None:
            key = cache_key(tile, snapshot_date)
            if not refresh_cache:
                cached = read_cached_response(cache_dir, key, CURRENT_CACHE_MAX_AGE if snapshot_date is None else None)
                if cached is not None:
                    return cached
        response = query_tile(endpoints, tile, timeout, retries, snapshot_date, deadline)
        if cache_dir is not None:
            write_cached_response(cache_dir, key, response)
        return response

    items = list(enumerate(tiles, start=1))
    if jobs <= 1:
        return [process(item) for item in items]
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        return list(executor.map(process, items))


def fetch_and_convert(*, bbox: list[float], snapshot_date: str | None, clip_geometry: Any | None, endpoints: Iterable[str], timeout: int, retries: int, deadline: float | None, tile_size: float, max_tiles: int, jobs: int, cache_dir: Path | None, refresh_cache: bool, building_values: set[str] | None, show_progress: bool, on_progress: Callable[[str], None] | None = None) -> tuple[dict[str, Any], list[list[float]], list[dict[str, Any]]]:
    """分块查询并转换为 GeoJSON，同时返回分块范围与原始响应，供命令行与适配器复用。"""
    tiles = split_bbox(bbox, tile_size, max_tiles)
    raw_responses = collect_tile_responses(endpoints, tiles, timeout, retries, snapshot_date, deadline, jobs, cache_dir, refresh_cache, show_progress, on_progress)
    elements = [element for response in raw_responses for element in response.get("elements", [])]
    data = convert_to_geojson(elements, bbox, snapshot_date, clip_geometry, building_values=building_values)
    data["metadata"]["query_tiles"] = len(tiles)
    return data, tiles, raw_responses


def polygon_from_ring(coordinates: list[dict[str, float]]) -> Polygon | None:
    points = [(point["lon"], point["lat"]) for point in coordinates]
    if len(points) < 3:
        return None
    polygon = Polygon(points)
    return polygon if polygon.is_valid and not polygon.is_empty else None


def relation_geometry(element: dict[str, Any]) -> Any | None:
    """把 relation 的 outer/inner 成员线拼接为带孔洞的几何，无法闭合时返回 None。"""
    outer_lines: list[LineString] = []
    inner_lines: list[LineString] = []
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


def convert_to_geojson(source: Iterable[dict[str, Any]], bbox: list[float], snapshot_date: str | None, clip_geometry: Any | None, building_values: set[str] | None = None) -> dict[str, Any]:
    """把 Overpass 元素转换为 GeoJSON，并在元数据中记录丢弃诊断。"""
    elements = list(source)
    # 只对实际保留的 relation 去重，避免筛选或裁剪导致成员建筑丢失。
    member_way_refs: set[int] = set()
    for element in elements:
        if element.get("type") != "relation" or (building_values and element.get("tags", {}).get("building") not in building_values):
            continue
        geometry = relation_geometry(element)
        if geometry is None:
            continue
        if clip_geometry is not None:
            geometry = geometry.intersection(clip_geometry)
        if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            continue
        member_way_refs.update(member["ref"] for member in element.get("members", []) if member.get("type") == "way" and isinstance(member.get("ref"), int))
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
        if building_values and element.get("tags", {}).get("building") not in building_values:
            diagnostics["filtered_elements"] += 1
            continue
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
    if not fields:
        fields = [("osm_id", "osm_id", "N", 10, 0)]
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


def geopackage_schema(features: list[dict[str, Any]]) -> dict[str, Any]:
    """按属性并集推导 GeoPackage schema，几何类型固定为 MultiPolygon。"""
    property_names = sorted({name for feature in features for name in feature["properties"]})
    properties = {}
    for name in property_names:
        values = [feature["properties"].get(name) for feature in features if feature["properties"].get(name) is not None]
        if values and all(isinstance(value, bool) for value in values):
            properties[name] = "int"
        elif values and all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            properties[name] = "int"
        elif values and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
            properties[name] = "float"
        else:
            properties[name] = "str"
    return {"geometry": "MultiPolygon", "properties": properties}


def write_geopackage(path: Path, data: dict[str, Any]) -> None:
    """把要素写入 GeoPackage，并生成与 SHP 侧车一致的元数据文件。"""
    if fiona is None:
        raise ValueError('Writing GeoPackage requires fiona. Install it with pip install "osm-building-downloader[gpkg]".')
    path.parent.mkdir(parents=True, exist_ok=True)
    features = data["features"]
    schema = geopackage_schema(features)
    try:
        with fiona.open(path, "w", driver="GPKG", schema=schema, crs="EPSG:4326", layer="buildings") as sink:
            for feature in features:
                geometry = shape(feature["geometry"])
                if geometry.geom_type == "Polygon":
                    geometry = MultiPolygon([geometry])
                properties = {name: feature["properties"].get(name) for name in schema["properties"]}
                sink.write({"geometry": mapping(geometry), "properties": properties})
    except fiona.errors.CRSError as error:
        raise ValueError("GeoPackage CRS error; check PROJ_DATA/PROJ_LIB for a conflicting PROJ installation: " + str(error)) from error
    path.with_suffix(".metadata.json").write_text(json.dumps(data["metadata"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_output(path: Path, data: dict[str, Any]) -> str:
    suffix = path.suffix.lower()
    if suffix in {".geojson", ".json"}:
        write_geojson(path, data)
        return "GeoJSON"
    if suffix == ".shp":
        write_shapefile(path, data)
        return "Shapefile"
    if suffix == ".gpkg":
        write_geopackage(path, data)
        return "GeoPackage"
    raise ValueError("--output must end in .geojson, .json, .shp, or .gpkg.")


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
        if args.deadline < 0:
            raise ValueError("--deadline must be zero or greater.")
        if not 1 <= args.jobs <= 8:
            raise ValueError("--jobs must be between 1 and 8.")
        building_values = {value.strip() for value in args.building_values.split(",") if value.strip()} if args.building_values else None
        snapshot_date = parse_snapshot_date(args.date)
        deadline = time.monotonic() + args.deadline if args.deadline > 0 else None
        endpoints = tuple(args.endpoint) if args.endpoint else DEFAULT_ENDPOINTS
        show_progress = len(split_bbox(bbox, args.tile_size, args.max_tiles)) > 1
        geojson, tiles, raw_responses = fetch_and_convert(
            bbox=bbox,
            snapshot_date=snapshot_date,
            clip_geometry=clip_geometry,
            endpoints=endpoints,
            timeout=args.timeout,
            retries=args.retries,
            deadline=deadline,
            tile_size=args.tile_size,
            max_tiles=args.max_tiles,
            jobs=args.jobs,
            cache_dir=args.cache_dir,
            refresh_cache=args.refresh_cache,
            building_values=building_values,
            show_progress=show_progress,
        )
    except (ValueError, RuntimeError, HTTPError, URLError, TimeoutError) as error:
        print(f"Download failed: {error}", file=sys.stderr)
        return 1
    if args.save_raw:
        args.save_raw.parent.mkdir(parents=True, exist_ok=True)
        args.save_raw.write_text(json.dumps({"responses": raw_responses}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        output_format = write_output(args.output, geojson)
    except (OSError, ValueError, shapefile.ShapefileException) as error:
        print(f"Output failed: {error}", file=sys.stderr)
        return 1
    diagnostics = geojson["metadata"]["diagnostics"]
    if any(diagnostics.values()):
        print("丢弃/过滤统计：" + ", ".join(f"{key}={value}" for key, value in diagnostics.items() if value), flush=True)
    print(f"Saved {len(geojson['features'])} building footprints as {output_format} from {len(tiles)} tile(s) to {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
