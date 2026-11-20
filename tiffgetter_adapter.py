#!/usr/bin/env python3
"""TiffGetter 契约适配器：按请求 JSON 调用下载逻辑并输出结果 JSON 与 NDJSON 事件。"""

from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import download_buildings as downloader

SCHEMA_VERSION = "1.0"
# 输出格式名与文件扩展名的固定映射
FORMAT_SUFFIXES = {"geojson": ".geojson", "shp": ".shp", "gpkg": ".gpkg"}


def utc_timestamp() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def zero_diagnostics() -> dict[str, int]:
    """返回全部诊断项为 0 的统计字典。"""
    return {key: 0 for key in downloader.DIAGNOSTIC_KEYS}


class EventWriter:
    """按行写入 NDJSON 事件，保证序号递增且每条记录立即刷新。"""

    def __init__(self, path: Path | None) -> None:
        self._sequence = 0
        self._lock = threading.Lock()
        self._handle: Any = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("w", encoding="utf-8")

    def emit(self, event_type: str, job_id: str, item: dict[str, Any]) -> None:
        """写入一条事件；未配置事件文件时不产生输出。"""
        with self._lock:
            if self._handle is None:
                return
            self._sequence += 1
            record = {"type": event_type, "schema_version": SCHEMA_VERSION, "job_id": job_id, "sequence": self._sequence, "timestamp": utc_timestamp(), "item": item}
            self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._handle.flush()

    def close(self) -> None:
        """关闭事件文件句柄。"""
        if self._handle is not None:
            self._handle.close()


def parse_request(payload: dict[str, Any]) -> dict[str, Any]:
    """校验请求 JSON 并返回规范化后的执行参数。"""
    if not isinstance(payload, dict):
        raise ValueError("请求必须是 JSON 对象。")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version 必须为 {SCHEMA_VERSION}。")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id 必须是非空字符串。")
    aoi = payload.get("aoi")
    if not isinstance(aoi, dict):
        raise ValueError("aoi 字段缺失或格式不正确。")
    bbox: list[float]
    clip_geometry: Any | None = None
    mode = aoi.get("mode")
    if mode == "bbox":
        raw_bbox = aoi.get("bbox")
        if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
            raise ValueError("aoi.bbox 必须是包含四个数值的列表。")
        bbox = [float(value) for value in raw_bbox]
        downloader.validate_bbox(bbox)
    elif mode == "shp":
        aoi_path = aoi.get("path")
        if not isinstance(aoi_path, str) or not aoi_path:
            raise ValueError("aoi.path 必须是非空字符串。")
        crs = aoi.get("crs")
        if crs is not None and not isinstance(crs, str):
            raise ValueError("aoi.crs 必须是字符串或 null。")
        clip_geometry = downloader.load_study_area(Path(aoi_path), crs)
        west, south, east, north = clip_geometry.bounds
        bbox = [south, west, north, east]
    else:
        raise ValueError("aoi.mode 必须为 bbox 或 shp。")
    output = payload.get("output")
    if not isinstance(output, dict):
        raise ValueError("output 字段缺失或格式不正确。")
    root = output.get("root")
    if not isinstance(root, str) or not root:
        raise ValueError("output.root 必须是非空字符串。")
    formats = output.get("formats", ["geojson"])
    if not isinstance(formats, list) or not formats:
        raise ValueError("output.formats 必须是非空列表。")
    for fmt in formats:
        if not isinstance(fmt, str) or fmt not in FORMAT_SUFFIXES:
            raise ValueError("output.formats 只能包含 geojson、shp 或 gpkg。")
    options = payload.get("options", {})
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError("options 必须是 JSON 对象。")
    jobs = options.get("jobs", 1)
    if not 1 <= jobs <= 8:
        raise ValueError("options.jobs 必须在 1 到 8 之间。")
    tile_size = options.get("tile_size", 0.05)
    if tile_size < 0:
        raise ValueError("options.tile_size 必须大于等于 0。")
    max_tiles = options.get("max_tiles", 2000)
    if max_tiles < 1:
        raise ValueError("options.max_tiles 必须大于等于 1。")
    retries = options.get("retries", 2)
    if retries < 0:
        raise ValueError("options.retries 必须大于等于 0。")
    timeout = options.get("timeout", 120)
    if timeout <= 0:
        raise ValueError("options.timeout 必须大于 0。")
    deadline = options.get("deadline", 0)
    if deadline < 0:
        raise ValueError("options.deadline 必须大于等于 0。")
    date_value = options.get("date")
    if date_value is not None and not isinstance(date_value, str):
        raise ValueError("options.date 必须是字符串或 null。")
    snapshot_date = downloader.parse_snapshot_date(date_value)
    raw_endpoints = options.get("endpoints", [])
    if not isinstance(raw_endpoints, list):
        raise ValueError("options.endpoints 必须是列表。")
    endpoints = tuple(raw_endpoints) if raw_endpoints else downloader.DEFAULT_ENDPOINTS
    cache_dir_value = options.get("cache_dir")
    if cache_dir_value is not None and not isinstance(cache_dir_value, str):
        raise ValueError("options.cache_dir 必须是字符串或 null。")
    cache_dir = Path(cache_dir_value) if cache_dir_value else None
    refresh_cache = options.get("refresh_cache", False)
    raw_values = options.get("building_values", [])
    if not isinstance(raw_values, list) or not all(isinstance(value, str) for value in raw_values):
        raise ValueError("options.building_values 必须是字符串列表。")
    building_values = {value.strip() for value in raw_values if value.strip()} or None
    return {
        "request_id": request_id,
        "bbox": bbox,
        "clip_geometry": clip_geometry,
        "root": root,
        "formats": formats,
        "snapshot_date": snapshot_date,
        "endpoints": endpoints,
        "timeout": timeout,
        "retries": retries,
        "deadline": deadline,
        "tile_size": tile_size,
        "max_tiles": max_tiles,
        "jobs": jobs,
        "cache_dir": cache_dir,
        "refresh_cache": refresh_cache,
        "building_values": building_values,
    }


def build_result(request_id: str, status: str, exit_code: int, started_at: str, items: list[dict[str, Any]], diagnostics: dict[str, int], detail: str | None) -> dict[str, Any]:
    """组装符合契约的顶层结果字典。"""
    return {"schema_version": SCHEMA_VERSION, "request_id": request_id, "status": status, "exit_code": exit_code, "started_at": started_at, "finished_at": utc_timestamp(), "items": items, "diagnostics": diagnostics, "detail": detail}


def run_request(config: dict[str, Any], events: EventWriter, started_at: str) -> dict[str, Any]:
    """执行一次下载请求，写出数据文件与元数据，并返回成功结果字典。"""
    request_id: str = config["request_id"]
    events.emit("log", request_id, {"message": "开始处理"})
    root = Path(config["root"])
    root.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + config["deadline"] if config["deadline"] > 0 else None

    def report(message: str) -> None:
        """把下载进度转发为 NDJSON progress 事件。"""
        events.emit("progress", request_id, {"message": message})

    data, _tiles, _responses = downloader.fetch_and_convert(
        bbox=config["bbox"],
        snapshot_date=config["snapshot_date"],
        clip_geometry=config["clip_geometry"],
        endpoints=config["endpoints"],
        timeout=config["timeout"],
        retries=config["retries"],
        deadline=deadline,
        tile_size=config["tile_size"],
        max_tiles=config["max_tiles"],
        jobs=config["jobs"],
        cache_dir=config["cache_dir"],
        refresh_cache=config["refresh_cache"],
        building_values=config["building_values"],
        show_progress=False,
        on_progress=report,
    )
    assets: dict[str, str | None] = {"geojson": None, "shp": None, "gpkg": None, "metadata": None}
    for name in ("geojson", "shp", "gpkg"):
        if name in config["formats"]:
            path = root / f"buildings{FORMAT_SUFFIXES[name]}"
            downloader.write_output(path, data)
            assets[name] = str(path)
    metadata_path = root / "buildings.metadata.json"
    metadata_path.write_text(json.dumps(data["metadata"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assets["metadata"] = str(metadata_path)
    metadata = data["metadata"]
    bounds_values = metadata["query_bbox_wgs84_south_west_north_east"]
    bounds = {"min_lon": bounds_values[1], "min_lat": bounds_values[0], "max_lon": bounds_values[3], "max_lat": bounds_values[2]}
    diagnostics = metadata["diagnostics"]
    item = {"aoi_id": request_id, "status": "success", "assets": assets, "bounds": bounds, "feature_count": len(data["features"]), "diagnostics": diagnostics, "detail": None}
    events.emit("log", request_id, {"message": "处理完成"})
    events.emit("run_summary", request_id, {"status": "success", "exit_code": 0, "feature_count": item["feature_count"], "diagnostics": diagnostics})
    return build_result(request_id, "success", 0, started_at, [item], diagnostics, None)


def write_result(path: Path, result: dict[str, Any]) -> None:
    """把结果 JSON 写入目标路径，父目录自动创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def finish(path: Path, result: dict[str, Any], exit_code: int) -> int:
    """写出结果 JSON；结果文件写入失败时统一返回退出码 4。"""
    try:
        write_result(path, result)
    except OSError:
        return 4
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """解析命令行参数，执行请求并写入结果与事件文件。"""
    parser = argparse.ArgumentParser(description="TiffGetter 建筑轮廓下载契约适配器")
    parser.add_argument("--request", type=Path, required=True, help="请求 JSON 路径。")
    parser.add_argument("--result", type=Path, required=True, help="结果 JSON 输出路径。")
    parser.add_argument("--events", type=Path, help="可选的事件 NDJSON 输出路径。")
    args = parser.parse_args(argv)
    started_at = utc_timestamp()
    payload: Any = None
    events = EventWriter(args.events)
    request_id = ""
    try:
        payload = json.loads(args.request.read_text(encoding="utf-8"))
        config = parse_request(payload)
    except (OSError, ValueError, TypeError) as error:
        if isinstance(payload, dict) and isinstance(payload.get("request_id"), str):
            request_id = payload["request_id"]
        events.emit("log", request_id, {"message": str(error)})
        events.emit("run_summary", request_id, {"status": "failed", "exit_code": 2, "feature_count": 0, "diagnostics": zero_diagnostics()})
        events.close()
        return finish(args.result, build_result(request_id, "failed", 2, started_at, [], zero_diagnostics(), str(error)), 2)
    request_id = config["request_id"]
    try:
        result = run_request(config, events, started_at)
    except Exception as error:
        events.emit("log", request_id, {"message": str(error)})
        events.emit("run_summary", request_id, {"status": "failed", "exit_code": 4, "feature_count": 0, "diagnostics": zero_diagnostics()})
        events.close()
        return finish(args.result, build_result(request_id, "failed", 4, started_at, [], zero_diagnostics(), str(error)), 4)
    events.close()
    return finish(args.result, result, 0)


if __name__ == "__main__":
    raise SystemExit(main())
