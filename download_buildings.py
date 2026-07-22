#!/usr/bin/env python3
"""Download OpenStreetMap building ways from an Overpass API endpoint as GeoJSON."""

from __future__ import annotations

import argparse
import json
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download OSM building polygons within a WGS84 bounding box.")
    extent = parser.add_mutually_exclusive_group(required=True)
    extent.add_argument("--bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"), help="WGS84 extent, ordered as south west north east.")
    extent.add_argument("--shp", type=Path, help="WGS84 shapefile whose bounding box defines the query extent.")
    parser.add_argument("--output", type=Path, required=True, help="Destination GeoJSON path.")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Overpass API URL.")
    parser.add_argument(
        "--date",
        help="Optional historical OSM snapshot time in ISO 8601 UTC format, for example 2024-01-01T00:00:00Z.",
    )
    parser.add_argument("--timeout", type=int, default=120, help="HTTP timeout in seconds.")
    parser.add_argument("--save-raw", type=Path, help="Optional path to save raw Overpass JSON.")
    return parser.parse_args()


def validate_bbox(bbox: list[float]) -> None:
    south, west, north, east = bbox
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise ValueError("Invalid bbox. Expected: -90 <= south < north <= 90 and -180 <= west < east <= 180.")


def bbox_from_shapefile(path: Path) -> list[float]:
    """Read the WGS84 bounding box from a standard ESRI Shapefile header."""
    try:
        with path.open("rb") as shp_file:
            header = shp_file.read(100)
    except OSError as error:
        raise ValueError(f"Could not read shapefile {path}: {error}") from error
    if len(header) != 100:
        raise ValueError(f"Invalid shapefile {path}: header is shorter than 100 bytes.")
    file_code = struct.unpack(">i", header[:4])[0]
    version = struct.unpack("<i", header[28:32])[0]
    if file_code != 9994 or version != 1000:
        raise ValueError(f"Invalid shapefile {path}: unsupported SHP header.")
    west, south, east, north = struct.unpack("<4d", header[36:68])
    bbox = [south, west, north, east]
    validate_bbox(bbox)
    return bbox


def parse_snapshot_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid --date. Use ISO 8601 UTC, for example 2024-01-01T00:00:00Z.") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("Invalid --date. Specify a UTC time ending in Z or +00:00.")
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def query_overpass(endpoint: str, bbox: list[float], timeout: int, snapshot_date: str | None) -> dict:
    south, west, north, east = bbox
    date_clause = f'[date:"{snapshot_date}"]' if snapshot_date else ""
    query = f'[out:json][timeout:{timeout}]{date_clause};way["building"]({south},{west},{north},{east});out geom;'
    request = Request(endpoint, data=urlencode({"data": query}).encode("utf-8"), headers={"User-Agent": "osm-building-downloader/1.0 (local data export)"})
    with urlopen(request, timeout=timeout + 15) as response:
        return json.load(response)


def convert_to_geojson(source: dict, bbox: list[float], snapshot_date: str | None) -> dict:
    features = []
    for element in source.get("elements", []):
        geometry = element.get("geometry", [])
        if element.get("type") != "way" or len(geometry) < 4:
            continue
        ring = [[point["lon"], point["lat"]] for point in geometry]
        if ring[0] != ring[-1]:
            ring.append(ring[0].copy())
        features.append({"type": "Feature", "properties": {"osm_type": element["type"], "osm_id": element["id"], **element.get("tags", {})}, "geometry": {"type": "Polygon", "coordinates": [ring]}})
    return {"type": "FeatureCollection", "metadata": {"source": "OpenStreetMap contributors, via Overpass API", "licence": "ODbL 1.0", "crs": "EPSG:4326 (WGS 84)", "query_bbox_wgs84_south_west_north_east": bbox, "requested_osm_snapshot_utc": snapshot_date, "osm_data_timestamp_utc": source.get("osm3s", {}).get("timestamp_osm_base"), "downloaded_at_utc": datetime.now(timezone.utc).isoformat(), "note": "Contains closed OSM building ways. Complex building relations are not included."}, "features": features}


def main() -> int:
    args = parse_args()
    try:
        bbox = bbox_from_shapefile(args.shp) if args.shp else args.bbox
        validate_bbox(bbox)
        snapshot_date = parse_snapshot_date(args.date)
        source = query_overpass(args.endpoint, bbox, args.timeout, snapshot_date)
    except (ValueError, HTTPError, URLError, TimeoutError) as error:
        print(f"Download failed: {error}", file=sys.stderr)
        return 1
    if args.save_raw:
        args.save_raw.parent.mkdir(parents=True, exist_ok=True)
        args.save_raw.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    geojson = convert_to_geojson(source, bbox, snapshot_date)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(geojson, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(geojson['features'])} building polygons to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
