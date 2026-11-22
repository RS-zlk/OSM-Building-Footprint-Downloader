#!/usr/bin/env python3
"""Modern desktop interface for the OSM building footprint downloader."""

from __future__ import annotations

import ctypes
import logging
import math
import os
import queue
import subprocess
import sys
import threading
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

if sys.platform == "win32":
    ctypes.windll.shcore.SetProcessDpiAwareness(2)


PROJECT_DIR = Path(__file__).resolve().parent
DOWNLOADER = PROJECT_DIR / "download_buildings.py"
TIFFGETTER_HOME = Path(os.environ.get("TIFFGETTER_HOME", str(PROJECT_DIR.parent / "TiffGetter")))
PRESETS = ("最新数据", "自定义日期时间", "1 天前", "7 天前", "30 天前", "90 天前", "1 年前")
OUTPUT_FORMATS = ("GeoJSON (.geojson)", "Shapefile (.shp)", "GeoPackage (.gpkg)")
PAPER = "#F3EEE4"
PAPER_LIGHT = "#FBF7EE"
CARD = "#F7F1E5"
INK = "#211D18"
INK_SOFT = "#4A4238"
MUTED = "#7A6F60"
RULE = "#D9CDBA"
RULE_STRONG = "#C4B49C"
OCHRE = "#A65A32"
OCHRE_DARK = "#7E4224"
MOSS = "#5F6B52"
ERROR = "#8C3A2B"
LOG_BG = "#221E19"
LOG_TEXT = "#EDE4D3"
UI_FAMILY = "Microsoft YaHei UI"
DISPLAY_FAMILY = "Georgia"
MONO_FAMILY = "Consolas"
CONTOUR_SEED = 20261120
FONT_SCALE = 1.3
MODE_TABS = (("经纬度", "bbox"), ("SHP 研究区", "shp"))
FORMAT_TABS = (("GeoJSON", OUTPUT_FORMATS[0]), ("Shapefile", OUTPUT_FORMATS[1]), ("GeoPackage", OUTPUT_FORMATS[2]))
PRESET_TABS = (("最新", "最新数据"), ("1 天", "1 天前"), ("7 天", "7 天前"), ("30 天", "30 天前"), ("90 天", "90 天前"), ("1 年", "1 年前"))
MAIN_TABS = (("buildings", "建筑轮廓"), ("basemap", "底图"), ("log", "日志"))
SOURCE_TABS = (("wayback", "Wayback"), ("esri", "ESRI"), ("bing", "Bing"), ("osm", "OSM"), ("google", "Google"))
BASEMAP_DEFAULT_SOURCE = "wayback"
BASEMAP_DEFAULT_ZOOM = "18"
BASEMAP_DEFAULT_OUTPUT = "data/basemap"


def _scaled(size: int) -> int:
    """按统一比例放大字号，供 Canvas 原生字体与控件尺寸使用。"""
    return max(7, round(size * FONT_SCALE))


def bbox_center_and_radius(bbox: tuple[float, float, float, float]) -> tuple[float, float, int]:
    """把研究区范围换算为 TiffGetter 需要的中心点与米制缓冲半径。"""
    south, west, north, east = bbox
    center_lat = (south + north) / 2
    center_lon = (west + east) / 2
    half_latitude = (north - south) / 2 * 111320.0
    half_longitude = (east - west) / 2 * 111320.0 * max(math.cos(math.radians(center_lat)), 1e-6)
    radius = max(50, int(math.ceil(math.hypot(half_longitude, half_latitude))))
    return center_lat, center_lon, radius


def load_tiffgetter() -> tuple[type, list[Any]]:
    """把 TiffGetter 源码目录加入模块搜索路径，返回 ImageFetcher 与数据源描述列表。"""
    home = str(TIFFGETTER_HOME)
    if home not in sys.path:
        sys.path.insert(0, home)
    from providers.registry import list_sources
    from satellite_processor import ImageFetcher

    return ImageFetcher, list_sources()


class QueueLogHandler(logging.Handler):
    """把 TiffGetter 的 logging 记录转发到界面事件队列。"""

    def __init__(self, events: queue.Queue[tuple[str, Any]]) -> None:
        super().__init__()
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        """把一条日志记录格式化后放入事件队列。"""
        self.events.put(("log", f"[底图] {self.format(record)}"))


def _hash_unit(ix: int, iy: int, seed: int) -> float:
    """返回基于整数格点的确定性伪随机值，范围 0 到 1。"""
    value = (ix * 374761393 + iy * 668265263 + seed * 1442695041) & 0xFFFFFFFF
    value = ((value ^ (value >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((value ^ (value >> 16)) & 0xFFFF) / 65535.0


def _value_noise(x: float, y: float, seed: int) -> float:
    """双线性插值的确定性值噪声，用于生成等高线扰动。"""
    ix, iy = math.floor(x), math.floor(y)
    fx, fy = x - ix, y - iy
    sx, sy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
    top = _hash_unit(ix, iy, seed) * (1 - sx) + _hash_unit(ix + 1, iy, seed) * sx
    bottom = _hash_unit(ix, iy + 1, seed) * (1 - sx) + _hash_unit(ix + 1, iy + 1, seed) * sx
    return top * (1 - sy) + bottom * sy


def _contour_polylines(width: int, height: int, seed: int = CONTOUR_SEED) -> list[list[tuple[float, float]]]:
    """围绕固定焦点生成被噪声扰动的同心等高线折线。"""
    polylines: list[list[tuple[float, float]]] = []
    foci = ((0.70, 0.52), (0.84, 0.28), (0.92, 0.74))
    for index, (cx, cy) in enumerate(foci):
        max_radius = height * (0.34 + index * 0.06)
        for level in range(1, 6):
            radius = max_radius * level / 5
            points: list[tuple[float, float]] = []
            for step in range(72):
                angle = math.tau * step / 72
                wobble = _value_noise(math.cos(angle) * 2.4 + index * 3.7, math.sin(angle) * 2.4 + level * 0.6, seed + level)
                ripple = _value_noise(math.cos(angle) * 5.0, math.sin(angle) * 5.0 + index * 1.9, seed + index)
                current = radius * (0.82 + 0.24 * wobble + 0.08 * ripple)
                points.append((width * cx + math.cos(angle) * current, height * cy + math.sin(angle) * current * 0.78))
            polylines.append(points)
    return polylines


def _draw_paper_contours(canvas: tk.Canvas, width: int, height: int, seed: int = CONTOUR_SEED) -> None:
    """在页眉画布右侧绘制低对比度等高线，作为程序化背景。"""
    if width < 760 or height < 24:
        return
    for index, points in enumerate(_contour_polylines(width, height, seed)):
        color = "#E8DDCB" if index % 2 == 0 else "#E1D4BF"
        coordinates = [value for point in points for value in point]
        canvas.create_line(*coordinates, fill=color, width=1, smooth=True)


def _draw_aoi_preview(canvas: tk.Canvas, bbox: tuple[float, float, float, float] | None) -> None:
    """绘制研究区预览：网格、范围矩形、四角刻线与经纬度标签。"""
    canvas.delete("all")
    width = canvas.winfo_width()
    height = canvas.winfo_height()
    if width <= 2 or height <= 2:
        return
    canvas.configure(bg=PAPER_LIGHT)
    for column in range(1, 6):
        x = width * column / 6
        canvas.create_line(x, 0, x, height, fill="#E8DDCB")
    for row in range(1, 3):
        y = height * row / 3
        canvas.create_line(0, y, width, y, fill="#E8DDCB")
    if bbox is None:
        canvas.create_text(width / 2, height / 2, text="等待有效范围", fill=MUTED, font=(MONO_FAMILY, -_scaled(7)))
        return
    south, west, north, east = bbox
    longitude_span = max(east - west, 1e-9)
    latitude_span = max(north - south, 1e-9)
    padding = 8
    scale = min((width - padding * 2) / longitude_span, (height - padding * 2) / latitude_span)
    rect_width = longitude_span * scale
    rect_height = latitude_span * scale
    left = (width - rect_width) / 2
    right = left + rect_width
    top = (height - rect_height) / 2
    bottom = top + rect_height
    canvas.create_rectangle(left, top, right, bottom, fill=OCHRE, stipple="gray12", outline=OCHRE, width=1)
    tick = 5
    canvas.create_line(left, top, left + tick, top, fill=OCHRE)
    canvas.create_line(left, top, left, top + tick, fill=OCHRE)
    canvas.create_line(right, top, right - tick, top, fill=OCHRE)
    canvas.create_line(right, top, right, top + tick, fill=OCHRE)
    canvas.create_line(left, bottom, left + tick, bottom, fill=OCHRE)
    canvas.create_line(left, bottom, left, bottom - tick, fill=OCHRE)
    canvas.create_line(right, bottom, right - tick, bottom, fill=OCHRE)
    canvas.create_line(right, bottom, right, bottom - tick, fill=OCHRE)
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    canvas.create_line(center_x - 4, center_y, center_x + 4, center_y, fill=OCHRE)
    canvas.create_line(center_x, center_y - 4, center_x, center_y + 4, fill=OCHRE)


def snapshot_to_iso(day: date, hour: str, minute: str, second: str) -> str:
    """Return a validated UTC timestamp from structured date/time controls."""
    try:
        value = datetime(day.year, day.month, day.day, int(hour), int(minute), int(second), tzinfo=timezone.utc)
    except ValueError as error:
        raise ValueError("历史快照时间无效。") from error
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot_text_to_iso(date_text: str, time_text: str) -> str:
    """Convert the date-picker values into the UTC format accepted by Overpass."""
    try:
        selected_date = datetime.strptime(date_text.strip(), "%Y-%m-%d").date()
        hour, minute, second = time_text.strip().split(":")
    except ValueError as error:
        raise ValueError("请按 YYYY-MM-DD 和 HH:MM:SS 格式填写历史快照。") from error
    return snapshot_to_iso(selected_date, hour, minute, second)


def available_days(year: str, month: str) -> list[str]:
    return [f"{value:02d}" for value in range(1, monthrange(int(year), int(month))[1] + 1)]


def output_extension(output_format: str) -> str:
    """返回所选输出格式对应的文件扩展名。"""
    if output_format == "Shapefile (.shp)":
        return ".shp"
    if output_format == "GeoPackage (.gpkg)":
        return ".gpkg"
    return ".geojson"


def tile_size_to_meters(tile_size: str, latitude: float) -> tuple[float, float]:
    """Estimate a degree-square tile's north/south and east/west size in metres."""
    try:
        degrees = float(tile_size)
    except ValueError as error:
        raise ValueError("网格大小必须是数字。") from error
    if degrees < 0:
        raise ValueError("网格大小不能小于 0。")
    latitude_radians = math.radians(latitude)
    metres_per_degree_latitude = 111132.92 - 559.82 * math.cos(2 * latitude_radians) + 1.175 * math.cos(4 * latitude_radians)
    metres_per_degree_longitude = 111412.84 * math.cos(latitude_radians) - 93.5 * math.cos(3 * latitude_radians)
    return degrees * metres_per_degree_latitude, degrees * metres_per_degree_longitude


def validate_download_inputs(source_mode: str, bbox: Sequence[str] | str, shp_path: str, output_path: str, tile_size: str, retries: str) -> None:
    """Validate GUI values early so users do not wait for a failed subprocess."""
    if source_mode == "bbox":
        values = bbox.replace(",", " ").split() if isinstance(bbox, str) else [value.strip() for value in bbox]
        if len(values) != 4 or not all(values):
            raise ValueError("请完整填写南、西、北、东四个范围值。")
        try:
            south, west, north, east = (float(value) for value in values)
        except (TypeError, ValueError) as error:
            raise ValueError("范围坐标必须是有效数字。") from error
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            raise ValueError("范围无效：需满足南 < 北、 西 < 东；纬度范围为 -90 到 90，经度范围为 -180 到 180。")
    else:
        path = Path(shp_path.strip())
        if not path.is_file():
            raise ValueError("请选择存在的研究区 SHP 文件。")
        if path.suffix.lower() != ".shp":
            raise ValueError("研究区文件必须是 .shp 格式。")

    if not output_path.strip():
        raise ValueError("请选择一个输出位置。")
    try:
        tile = float(tile_size)
        retry_count = int(retries)
    except ValueError as error:
        raise ValueError("网格大小必须是数字，重试次数必须是整数。") from error
    if tile < 0:
        raise ValueError("网格大小不能小于 0（0 表示不分块）。")
    if retry_count < 0:
        raise ValueError("重试次数不能小于 0。")


def downloader_command_prefix() -> list[str]:
    """返回启动下载程序的命令前缀：打包运行时使用同目录的 CLI 可执行文件。"""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).with_name("osm-buildings-cli.exe"))]
    return [sys.executable, str(DOWNLOADER)]


def build_command(source_mode: str, bbox: Sequence[str] | str, shp_path: str, output_path: str, snapshot_date: str, tile_size: str, retries: str, building_values: str = "", endpoints: str = "") -> list[str]:
    """Build a safe subprocess command from validated GUI field values."""
    command = downloader_command_prefix()
    if source_mode == "shp":
        if not shp_path.strip():
            raise ValueError("请选择研究区 SHP 文件。")
        command.extend(["--shp", shp_path.strip()])
    else:
        values = bbox.replace(",", " ").split() if isinstance(bbox, str) else [value.strip() for value in bbox]
        if len(values) != 4 or not all(values):
            raise ValueError("请完整填写南、西、北、东四个范围值。")
        command.extend(["--bbox", *values])
    if not output_path.strip():
        raise ValueError("请选择输出文件。")
    command.extend(["--output", output_path.strip()])
    if snapshot_date:
        command.extend(["--date", snapshot_date])
    if tile_size.strip():
        command.extend(["--tile-size", tile_size.strip()])
    if retries.strip():
        command.extend(["--retries", retries.strip()])
    if building_values.strip():
        command.extend(["--building-values", building_values.strip()])
    for endpoint in endpoints.split(","):
        value = endpoint.strip()
        if value:
            command.extend(["--endpoint", value])
    return command


class DownloaderApp(ctk.CTkFrame):
    def __init__(self, master: ctk.CTk) -> None:
        super().__init__(master, fg_color=PAPER, corner_radius=0)
        self.master = master
        now = datetime.now(timezone.utc)
        self.source_mode = tk.StringVar(value="bbox")
        self.coordinates = {name: tk.StringVar(value=value) for name, value in (("南", "30.742"), ("西", "120.480"), ("北", "30.750"), ("东", "120.490"))}
        self.shp_path = tk.StringVar()
        self.output_path = tk.StringVar(value="data/buildings.geojson")
        self.output_format = tk.StringVar(value=OUTPUT_FORMATS[0])
        self.snapshot_preset = tk.StringVar(value="最新数据")
        self.snapshot_date_text = tk.StringVar(value=now.strftime("%Y-%m-%d"))
        self.snapshot_time_text = tk.StringVar(value="00:00:00")
        self.calendar_month = date(now.year, now.month, 1)
        self.tile_size = tk.StringVar(value="0.05")
        self.retries = tk.StringVar(value="2")
        self.building_values = tk.StringVar()
        self.endpoints = tk.StringVar()
        self.basemap_source = tk.StringVar(value=BASEMAP_DEFAULT_SOURCE)
        self.basemap_zoom = tk.StringVar(value=BASEMAP_DEFAULT_ZOOM)
        self.basemap_date_start = tk.StringVar()
        self.basemap_date_end = tk.StringVar()
        self.basemap_output = tk.StringVar(value=BASEMAP_DEFAULT_OUTPUT)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.running = False
        self.cancel_requested = False
        self.process: subprocess.Popen[str] | None = None
        self.advanced_dialog: ctk.CTkToplevel | None = None
        self._tabs: dict[str, dict[str, tuple[ctk.CTkFrame, ctk.CTkButton, ctk.CTkFrame]]] = {}
        self._tab_meta: dict[str, dict[str, Any]] = {}
        self._pages: dict[str, ctk.CTkFrame] = {}
        self._active_page = "buildings"
        self._basemap_infos: dict[str, Any] = {}
        self._preview_job: str | None = None
        self._header_job: str | None = None
        self.tile_size.trace_add("write", self._update_tile_meter_hint)
        self.coordinates["南"].trace_add("write", self._update_tile_meter_hint)
        self.coordinates["北"].trace_add("write", self._update_tile_meter_hint)
        self._build()
        for name in ("南", "西", "北", "东"):
            self.coordinates[name].trace_add("write", self._schedule_preview)
            self.coordinates[name].trace_add("write", self._update_basemap_summary)
        self.shp_path.trace_add("write", self._update_basemap_summary)
        for variable in (self.tile_size, self.retries, self.building_values, self.endpoints):
            variable.trace_add("write", self._update_advanced_summary)
        self._update_source_state()
        self._update_snapshot_state()
        self._update_tile_meter_hint()
        self._update_advanced_summary()
        self._update_basemap_summary()
        self.after(100, self._handle_events)

    def _ui_font(self, size: int, weight: str = "normal") -> ctk.CTkFont:
        """返回放大后的中文界面字体。"""
        return ctk.CTkFont(family=UI_FAMILY, size=_scaled(size), weight=weight)

    def _display_font(self, size: int, weight: str = "normal") -> ctk.CTkFont:
        """返回放大后用于数字和英文展示的衬线字体。"""
        return ctk.CTkFont(family=DISPLAY_FAMILY, size=_scaled(size), weight=weight)

    def _mono_font(self, size: int, weight: str = "normal") -> ctk.CTkFont:
        """返回放大后用于坐标与日志的等宽字体。"""
        return ctk.CTkFont(family=MONO_FAMILY, size=_scaled(size), weight=weight)

    def _apply_window_geometry(self) -> None:
        """按 DPI 缩放计算窗口的逻辑尺寸，并用物理尺寸居中，确保整窗落在屏幕内。"""
        scale = max(1.0, ctk.ScalingTracker.get_window_scaling(self.master), ctk.ScalingTracker.get_widget_scaling(self.master))
        screen_width = self.master.winfo_screenwidth()
        screen_height = self.master.winfo_screenheight()
        usable_height = screen_height - round(48 * scale)
        width = max(1120, int(min(1260, screen_width / scale - 20)))
        height = max(660, int(min(776, usable_height / scale - 40)))
        physical_width = round(width * scale)
        physical_height = round(height * scale)
        frame_width = round(16 * scale)
        frame_height = round(40 * scale)
        left = max(0, (screen_width - physical_width - frame_width) // 2)
        top = max(0, (usable_height - physical_height - frame_height) // 2)
        self.master.geometry(f"{width}x{height}+{left}+{top}")
        self.master.minsize(1120, 660)

    def _build(self) -> None:
        """构建页眉、主标签行、三个页面与页脚。"""
        self.master.title("OSM 建筑轮廓下载器")
        self.master.configure(fg_color=PAPER)
        self._apply_window_geometry()
        self.grid(sticky="nsew")
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=0)
        self.rowconfigure(1, weight=0)
        self.rowconfigure(2, weight=1)
        self.rowconfigure(3, weight=0)
        self._build_header()
        self._build_main_tabs()
        self._build_pages()
        self._build_footer()

    def _build_header(self) -> None:
        """构建带程序化等高线背景的页眉画布。"""
        self.header_canvas = tk.Canvas(self, height=104, bg=PAPER, highlightthickness=0)
        self.header_canvas.grid(row=0, column=0, sticky="ew")
        self.header_canvas.bind("<Configure>", self._on_header_configure)

    def _on_header_configure(self, event: tk.Event) -> None:
        """页眉尺寸变化后防抖重绘。"""
        if self._header_job is not None:
            self.after_cancel(self._header_job)
        self._header_job = self.after(60, lambda: self._draw_header(event.width, event.height))

    def _draw_header(self, width: int, height: int) -> None:
        """绘制页眉文字与等高线。"""
        self._header_job = None
        canvas = self.header_canvas
        canvas.delete("all")
        _draw_paper_contours(canvas, width, height)
        canvas.create_text(28, 8, text="OPENSTREETMAP · BUILDING FOOTPRINTS", anchor="nw", fill=OCHRE, font=(MONO_FAMILY, -_scaled(8)))
        canvas.create_text(28, 24, text="OSM", anchor="nw", fill=INK_SOFT, font=(DISPLAY_FAMILY, -_scaled(24), "italic"))
        canvas.create_text(126, 26, text="建筑轮廓下载器", anchor="nw", fill=INK, font=(UI_FAMILY, -_scaled(20), "bold"))
        canvas.create_text(28, 80, text="范围 · 时间 · 格式，一次导出可用于 GIS 的建筑数据与影像底图", anchor="nw", fill=MUTED, font=(UI_FAMILY, -_scaled(8)))
        canvas.create_text(width - 28, 10, text="WGS84", anchor="ne", fill=INK_SOFT, font=(MONO_FAMILY, -_scaled(8)))
        canvas.create_text(width - 28, 30, text="OVERPASS API", anchor="ne", fill=MUTED, font=(MONO_FAMILY, -_scaled(8)))
        canvas.create_text(width - 28, 50, text="ODbL", anchor="ne", fill=MUTED, font=(MONO_FAMILY, -_scaled(8)))
        canvas.create_line(28, height - 1, width - 28, height - 1, fill=RULE)

    def _build_main_tabs(self) -> None:
        """构建建筑轮廓、底图、日志三个主标签。"""
        bar = ctk.CTkFrame(self, fg_color=PAPER)
        bar.grid(row=1, column=0, sticky="ew", padx=28, pady=(4, 0))
        for key, label in MAIN_TABS:
            holder = self._tab(bar, label, lambda selected=key: self._show_page(selected), "page", key, 116, key == self._active_page, line_idle=PAPER, font_size=12, height=34, line_height=3)
            holder.pack(side="left", padx=(0, 18))

    def _build_pages(self) -> None:
        """构建并叠放三个主页面，切换时提升对应页面。"""
        for key, _label in MAIN_TABS:
            page = ctk.CTkFrame(self, fg_color=PAPER)
            page.grid(row=2, column=0, sticky="nsew", padx=28, pady=(10, 6))
            self._pages[key] = page
        self._build_buildings_page()
        self._build_basemap_page()
        self._build_log_page()
        self._show_page(self._active_page)

    def _show_page(self, key: str) -> None:
        """切换到指定主标签页面。"""
        self._active_page = key
        self._pages[key].lift()
        self._set_tab_active("page", key)
        if key == "log" and hasattr(self, "log"):
            self.log.see("end")

    def _build_buildings_page(self) -> None:
        """构建建筑轮廓页：左侧范围与时间，右侧导出。"""
        page = self._pages["buildings"]
        page.columnconfigure(0, weight=3)
        page.columnconfigure(1, weight=2, minsize=440)
        page.rowconfigure(0, weight=1)
        left = ctk.CTkFrame(page, fg_color=PAPER)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        left.columnconfigure(0, weight=1)
        self._build_range_card(left)
        self._build_time_card(left)
        right = ctk.CTkFrame(page, fg_color=PAPER)
        right.grid(row=0, column=1, sticky="new")
        right.columnconfigure(0, weight=1)
        self._build_output_card(right)

    def _card(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        """创建纸色卡片容器。"""
        return ctk.CTkFrame(parent, fg_color=CARD, border_color=RULE, border_width=1, corner_radius=2)

    def _section_header(self, parent: ctk.CTkFrame, index: str, title: str, subtitle: str) -> None:
        """绘制带章节编号的标题区。"""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(9, 4))
        ctk.CTkLabel(row, text=index, text_color=OCHRE, font=self._display_font(24)).pack(side="left", anchor="n")
        text_column = ctk.CTkFrame(row, fg_color="transparent")
        text_column.pack(side="left", padx=(8, 0), anchor="n")
        ctk.CTkLabel(text_column, text=title, text_color=INK, font=self._ui_font(13, "bold")).pack(side="left")
        if subtitle:
            ctk.CTkLabel(text_column, text=subtitle, text_color=MUTED, font=self._ui_font(8)).pack(side="left", padx=(8, 0))

    def _hairline(self, parent: ctk.CTkFrame, pady: tuple[int, int] = (0, 0)) -> None:
        """插入一条发丝分隔线。"""
        ctk.CTkFrame(parent, height=1, fg_color=RULE, corner_radius=0).pack(fill="x", padx=12, pady=pady)

    def _tab(self, parent: ctk.CTkFrame, text: str, command, group: str, key: str, width: int, active: bool = False, line_idle: str = CARD, font_size: int = 9, height: int = 26, line_height: int = 2) -> ctk.CTkFrame:
        """创建一个带下划线状态的文字切换按钮。"""
        self._tab_meta[group] = {"line_idle": line_idle, "font_size": font_size, "line_height": line_height}
        holder = ctk.CTkFrame(parent, fg_color="transparent")
        button = ctk.CTkButton(holder, text=text, width=width, height=height, fg_color="transparent", hover_color="#EDE4D3", text_color=OCHRE if active else MUTED, font=self._ui_font(font_size, "bold" if active else "normal"), corner_radius=0, border_width=0, command=command)
        button.pack()
        line = ctk.CTkFrame(holder, width=1, height=line_height, fg_color=OCHRE if active else line_idle, corner_radius=0)
        line.pack(fill="x", padx=2)
        self._tabs.setdefault(group, {})[key] = (holder, button, line)
        return holder

    def _set_tab_active(self, group: str, key: str) -> None:
        """切换同一组文字按钮的激活样式。"""
        meta = self._tab_meta.get(group, {"line_idle": CARD, "font_size": 9, "line_height": 2})
        for current, (_holder, button, line) in self._tabs.get(group, {}).items():
            active = current == key
            button.configure(text_color=OCHRE if active else MUTED, font=self._ui_font(meta["font_size"], "bold" if active else "normal"))
            line.configure(fg_color=OCHRE if active else meta["line_idle"])

    def _build_range_card(self, parent: ctk.CTkFrame) -> None:
        card = self._card(parent)
        card.grid(row=0, column=0, sticky="ew")
        self._section_header(card, "01", "范围", "输入 WGS84 坐标，或选择一个 SHP 研究区。")
        modes = ctk.CTkFrame(card, fg_color="transparent")
        modes.pack(fill="x", padx=12, pady=(0, 4))
        for label, value in MODE_TABS:
            holder = self._tab(modes, label, lambda selected=value: self._select_source(selected), "source", value, 72, value == self.source_mode.get())
            holder.pack(side="left", padx=(0, 8))
        coordinates = ctk.CTkFrame(card, fg_color="transparent")
        coordinates.pack(fill="x", padx=12)
        coordinates.columnconfigure(0, weight=1)
        coordinates.columnconfigure(1, weight=1)
        self.coordinate_entries: list[ctk.CTkEntry] = []
        for index, (name, suffix) in enumerate((("南", "S"), ("西", "W"), ("北", "N"), ("东", "E"))):
            row, column = divmod(index, 2)
            field = ctk.CTkFrame(coordinates, fg_color="transparent")
            field.grid(row=row, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0), pady=(0, 6))
            ctk.CTkLabel(field, text=f"{name} {suffix}", text_color=MUTED, font=self._mono_font(8), height=16).pack(anchor="w", pady=(0, 2))
            entry = ctk.CTkEntry(field, textvariable=self.coordinates[name], fg_color=PAPER_LIGHT, border_color=RULE_STRONG, border_width=1, text_color=INK, corner_radius=2, height=34, font=self._mono_font(10))
            entry.pack(fill="x")
            entry.bind("<FocusIn>", lambda _event, widget=entry: widget.configure(border_color=OCHRE))
            entry.bind("<FocusOut>", lambda _event, widget=entry: widget.configure(border_color=RULE_STRONG))
            self.coordinate_entries.append(entry)
        shp_row = ctk.CTkFrame(card, fg_color="transparent")
        shp_row.pack(fill="x", padx=12, pady=(2, 0))
        self.shp_entry = ctk.CTkEntry(shp_row, textvariable=self.shp_path, placeholder_text="选择 .shp 研究区文件", fg_color=PAPER_LIGHT, border_color=RULE_STRONG, border_width=1, text_color=INK, corner_radius=2, height=34, font=self._mono_font(9))
        self.shp_entry.pack(side="left", fill="x", expand=True)
        self.shp_button = ctk.CTkButton(shp_row, text="浏览…", width=64, height=34, fg_color=PAPER_LIGHT, border_width=1, border_color=RULE_STRONG, text_color=INK, hover_color="#EDE4D3", corner_radius=2, font=self._ui_font(9), command=self._choose_shp)
        self.shp_button.pack(side="left", padx=(8, 0))
        self.preview_canvas = tk.Canvas(card, height=48, bg=PAPER_LIGHT, highlightthickness=1, highlightbackground=RULE)
        self.preview_canvas.pack(fill="x", padx=12, pady=(8, 8))
        self.preview_canvas.bind("<Configure>", lambda _event: self._refresh_preview())

    def _build_time_card(self, parent: ctk.CTkFrame) -> None:
        card = self._card(parent)
        card.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self._section_header(card, "02", "时间", "")
        presets = ctk.CTkFrame(card, fg_color="transparent")
        presets.pack(fill="x", padx=12, pady=(0, 4))
        for label, value in PRESET_TABS:
            holder = self._tab(presets, label, lambda selected=value: self._select_preset(selected), "preset", value, 40, value == self.snapshot_preset.get())
            holder.pack(side="left", padx=(0, 4))
        date_row = ctk.CTkFrame(card, fg_color="transparent")
        date_row.pack(fill="x", padx=12, pady=(0, 6))
        date_row.columnconfigure(0, weight=3)
        date_row.columnconfigure(1, weight=2)
        ctk.CTkLabel(date_row, text="日期", text_color=MUTED, font=self._mono_font(8), height=16).grid(row=0, column=0, sticky="w", pady=(0, 2))
        ctk.CTkLabel(date_row, text="时间（UTC）", text_color=MUTED, font=self._mono_font(8), height=16).grid(row=0, column=1, sticky="w", padx=(8, 0), pady=(0, 2))
        self.snapshot_date_entry = ctk.CTkEntry(date_row, textvariable=self.snapshot_date_text, placeholder_text="YYYY-MM-DD", fg_color=PAPER_LIGHT, border_color=RULE_STRONG, border_width=1, text_color=INK, corner_radius=2, height=34, font=self._mono_font(10))
        self.snapshot_date_entry.grid(row=1, column=0, sticky="ew")
        self.snapshot_time_entry = ctk.CTkEntry(date_row, textvariable=self.snapshot_time_text, placeholder_text="HH:MM:SS", fg_color=PAPER_LIGHT, border_color=RULE_STRONG, border_width=1, text_color=INK, corner_radius=2, height=34, font=self._mono_font(10))
        self.snapshot_time_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        self.calendar_button = ctk.CTkButton(date_row, text="日历", width=56, height=34, fg_color=PAPER_LIGHT, border_width=1, border_color=RULE_STRONG, text_color=INK, hover_color="#EDE4D3", corner_radius=2, font=self._ui_font(9), command=self._open_date_picker)
        self.calendar_button.grid(row=1, column=2, padx=(8, 0))
        self.snapshot_status = ctk.CTkLabel(card, text="UTC · 最新数据", text_color=MUTED, font=self._mono_font(8), height=16)
        self.snapshot_status.pack(anchor="w", padx=12, pady=(0, 8))

    def _build_output_card(self, parent: ctk.CTkFrame) -> None:
        """构建建筑数据导出卡片。"""
        card = self._card(parent)
        card.pack(fill="x")
        self._section_header(card, "03", "导出", "")
        formats = ctk.CTkFrame(card, fg_color="transparent")
        formats.pack(fill="x", padx=12, pady=(0, 6))
        for label, value in FORMAT_TABS:
            holder = self._tab(formats, label, lambda selected=value: self._select_format(selected), "format", value, 88, value == self.output_format.get())
            holder.pack(side="left", padx=(0, 6))
        ctk.CTkLabel(card, text="输出文件", text_color=MUTED, font=self._mono_font(8)).pack(anchor="w", padx=12, pady=(0, 3))
        path_row = ctk.CTkFrame(card, fg_color="transparent")
        path_row.pack(fill="x", padx=12)
        path_row.columnconfigure(0, weight=1)
        self.output_entry = ctk.CTkEntry(path_row, textvariable=self.output_path, fg_color=PAPER_LIGHT, border_color=RULE_STRONG, border_width=1, text_color=INK, corner_radius=2, height=34, font=self._mono_font(9))
        self.output_entry.grid(row=0, column=0, sticky="ew")
        self.output_entry._entry.configure(justify="left")
        self.output_path.trace_add("write", self._show_output_path_from_start)
        ctk.CTkButton(path_row, text="选择…", width=64, height=34, fg_color=PAPER_LIGHT, border_width=1, border_color=RULE_STRONG, text_color=INK, hover_color="#EDE4D3", corner_radius=2, font=self._ui_font(9), command=self._choose_output).grid(row=0, column=1, padx=(8, 0))
        self.download_button = ctk.CTkButton(card, text="开始下载", height=46, fg_color=INK, hover_color="#3A332A", text_color=PAPER_LIGHT, corner_radius=2, font=self._ui_font(13, "bold"), command=self._start_download)
        self.download_button.pack(fill="x", padx=12, pady=(10, 4))
        self.cancel_button = ctk.CTkButton(card, text="取消下载", height=24, fg_color="transparent", hover_color="#EDE4D3", text_color=MUTED, text_color_disabled=RULE_STRONG, corner_radius=0, border_width=0, font=self._ui_font(9), state="disabled", command=self._cancel_download)
        self.cancel_button.pack()
        self.status_label = ctk.CTkLabel(card, text="就绪", text_color=MUTED, font=self._mono_font(8), height=16)
        self.status_label.pack(anchor="w", padx=12, pady=(0, 3))
        self.progress = ctk.CTkProgressBar(card, mode="determinate", height=4, progress_color=OCHRE, fg_color=RULE, corner_radius=0)
        self.progress.pack(fill="x", padx=12, pady=(0, 8))
        self.progress.set(0)
        self._hairline(card, (2, 8))
        advanced_row = ctk.CTkFrame(card, fg_color="transparent")
        advanced_row.pack(fill="x", padx=12)
        ctk.CTkLabel(advanced_row, text="高级选项", text_color=INK, font=self._ui_font(9, "bold")).pack(side="left")
        ctk.CTkButton(advanced_row, text="设置", width=56, height=26, fg_color="transparent", hover_color="#EDE4D3", text_color=OCHRE, corner_radius=0, border_width=0, font=self._ui_font(9), command=self._open_advanced_dialog).pack(side="right")
        self.advanced_summary = ctk.CTkLabel(card, text="网格 0.05° · 重试 2 · 端点默认", text_color=MUTED, font=self._mono_font(8), anchor="w", wraplength=420)
        self.advanced_summary.pack(fill="x", padx=12, pady=(0, 3))
        self.tile_meter_label = ctk.CTkLabel(card, text="", text_color=MUTED, font=self._mono_font(8), anchor="w", wraplength=420)
        self.tile_meter_label.pack(fill="x", padx=12, pady=(0, 10))

    def _build_basemap_page(self) -> None:
        """构建底图页：左侧下载参数，右侧覆盖信息。"""
        self._basemap_error: str | None = None
        try:
            _fetcher, infos = load_tiffgetter()
            self._basemap_infos = {info.name: info for info in infos}
        except ImportError as error:
            self._basemap_error = str(error)
        page = self._pages["basemap"]
        page.columnconfigure(0, weight=3)
        page.columnconfigure(1, weight=2, minsize=440)
        page.rowconfigure(0, weight=1)
        left = ctk.CTkFrame(page, fg_color=PAPER)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        left.columnconfigure(0, weight=1)
        right = ctk.CTkFrame(page, fg_color=PAPER)
        right.grid(row=0, column=1, sticky="new")
        right.columnconfigure(0, weight=1)
        self._build_basemap_form(left)
        self._build_basemap_info(right)
        if self._basemap_error:
            self.basemap_status.configure(text="底图未配置：请参阅 README 安装 TiffGetter", text_color=ERROR)

    def _build_basemap_form(self, parent: ctk.CTkFrame) -> None:
        """构建底图下载参数卡片。"""
        card = self._card(parent)
        card.grid(row=0, column=0, sticky="ew")
        self._section_header(card, "01", "底图数据源", "由 TiffGetter 提供，覆盖当前研究区。")
        sources = ctk.CTkFrame(card, fg_color="transparent")
        sources.pack(fill="x", padx=12, pady=(0, 6))
        for name, label in SOURCE_TABS:
            holder = self._tab(sources, label, lambda selected=name: self._select_basemap_source(selected), "basemap_source", name, 96, name == self.basemap_source.get())
            holder.pack(side="left", padx=(0, 6))
        parameters = ctk.CTkFrame(card, fg_color="transparent")
        parameters.pack(fill="x", padx=12, pady=(0, 6))
        parameters.columnconfigure(0, weight=1)
        parameters.columnconfigure(1, weight=2)
        parameters.columnconfigure(2, weight=2)
        ctk.CTkLabel(parameters, text="缩放级别", text_color=MUTED, font=self._mono_font(8), height=16).grid(row=0, column=0, sticky="w", pady=(0, 2))
        ctk.CTkLabel(parameters, text="开始日期", text_color=MUTED, font=self._mono_font(8), height=16).grid(row=0, column=1, sticky="w", padx=(8, 0), pady=(0, 2))
        ctk.CTkLabel(parameters, text="结束日期", text_color=MUTED, font=self._mono_font(8), height=16).grid(row=0, column=2, sticky="w", padx=(8, 0), pady=(0, 2))
        self.basemap_zoom_entry = ctk.CTkEntry(parameters, textvariable=self.basemap_zoom, fg_color=PAPER_LIGHT, border_color=RULE_STRONG, border_width=1, text_color=INK, corner_radius=2, height=34, font=self._mono_font(10))
        self.basemap_zoom_entry.grid(row=1, column=0, sticky="ew")
        self.basemap_date_start_entry = ctk.CTkEntry(parameters, textvariable=self.basemap_date_start, placeholder_text="YYYY-MM-DD", fg_color=PAPER_LIGHT, border_color=RULE_STRONG, border_width=1, text_color=INK, corner_radius=2, height=34, font=self._mono_font(9))
        self.basemap_date_start_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        self.basemap_date_end_entry = ctk.CTkEntry(parameters, textvariable=self.basemap_date_end, placeholder_text="YYYY-MM-DD", fg_color=PAPER_LIGHT, border_color=RULE_STRONG, border_width=1, text_color=INK, corner_radius=2, height=34, font=self._mono_font(9))
        self.basemap_date_end_entry.grid(row=1, column=2, sticky="ew", padx=(8, 0))
        self.basemap_date_hint = ctk.CTkLabel(card, text="", text_color=MUTED, font=self._mono_font(8), anchor="w", wraplength=560, justify="left")
        self.basemap_date_hint.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(card, text="输出目录", text_color=MUTED, font=self._mono_font(8)).pack(anchor="w", padx=12, pady=(0, 3))
        output_row = ctk.CTkFrame(card, fg_color="transparent")
        output_row.pack(fill="x", padx=12)
        output_row.columnconfigure(0, weight=1)
        self.basemap_output_entry = ctk.CTkEntry(output_row, textvariable=self.basemap_output, fg_color=PAPER_LIGHT, border_color=RULE_STRONG, border_width=1, text_color=INK, corner_radius=2, height=34, font=self._mono_font(9))
        self.basemap_output_entry.grid(row=0, column=0, sticky="ew")
        self.basemap_output_entry._entry.configure(justify="left")
        ctk.CTkButton(output_row, text="选择…", width=64, height=34, fg_color=PAPER_LIGHT, border_width=1, border_color=RULE_STRONG, text_color=INK, hover_color="#EDE4D3", corner_radius=2, font=self._ui_font(9), command=self._choose_basemap_output).grid(row=0, column=1, padx=(8, 0))
        self.basemap_button = ctk.CTkButton(card, text="下载底图", height=46, fg_color=OCHRE, hover_color=OCHRE_DARK, text_color=PAPER_LIGHT, corner_radius=2, font=self._ui_font(13, "bold"), command=self._start_basemap_download)
        self.basemap_button.pack(fill="x", padx=12, pady=(12, 4))
        self.basemap_status = ctk.CTkLabel(card, text="就绪", text_color=MUTED, font=self._mono_font(8), height=16)
        self.basemap_status.pack(anchor="w", padx=12, pady=(0, 3))
        self.basemap_progress = ctk.CTkProgressBar(card, mode="determinate", height=4, progress_color=OCHRE, fg_color=RULE, corner_radius=0)
        self.basemap_progress.pack(fill="x", padx=12, pady=(0, 10))
        self.basemap_progress.set(0)

    def _build_basemap_info(self, parent: ctk.CTkFrame) -> None:
        """构建底图覆盖信息卡片。"""
        card = self._card(parent)
        card.grid(row=0, column=0, sticky="ew")
        self._section_header(card, "02", "覆盖信息", "")
        ctk.CTkLabel(card, text="中心点（纬度, 经度）", text_color=MUTED, font=self._mono_font(8)).pack(anchor="w", padx=12, pady=(0, 2))
        self.basemap_center_label = ctk.CTkLabel(card, text="等待有效范围", text_color=INK, font=self._display_font(15, "bold"), anchor="w")
        self.basemap_center_label.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(card, text="覆盖半径（按范围半对角线换算）", text_color=MUTED, font=self._mono_font(8)).pack(anchor="w", padx=12, pady=(0, 2))
        self.basemap_radius_label = ctk.CTkLabel(card, text="—", text_color=INK, font=self._display_font(15, "bold"), anchor="w")
        self.basemap_radius_label.pack(fill="x", padx=12, pady=(0, 8))
        self._hairline(card, (2, 8))
        ctk.CTkLabel(card, text="数据源能力", text_color=MUTED, font=self._mono_font(8)).pack(anchor="w", padx=12, pady=(0, 2))
        self.basemap_source_info = ctk.CTkLabel(card, text="", text_color=INK_SOFT, font=self._mono_font(9), anchor="w", wraplength=420, justify="left")
        self.basemap_source_info.pack(fill="x", padx=12, pady=(0, 6))
        self.basemap_source_note = ctk.CTkLabel(card, text="", text_color=MUTED, font=self._mono_font(8), anchor="w", wraplength=420, justify="left")
        self.basemap_source_note.pack(fill="x", padx=12, pady=(0, 10))

    def _build_log_page(self) -> None:
        """构建日志页：占满页面的运行日志。"""
        page = self._pages["log"]
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=1)
        card = self._card(page)
        card.grid(row=0, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)
        title_row = ctk.CTkFrame(card, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 6))
        ctk.CTkLabel(title_row, text="运行日志", text_color=INK, font=self._ui_font(11, "bold")).pack(side="left")
        ctk.CTkLabel(title_row, text="LIVE", text_color=OCHRE, font=self._mono_font(8)).pack(side="right")
        self.log = ctk.CTkTextbox(card, fg_color=LOG_BG, border_width=0, corner_radius=2, text_color=LOG_TEXT, font=self._mono_font(9), wrap="word")
        self.log.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 12))
        self._write_log("就绪：请选择范围、快照和保存格式。")

    def _select_basemap_source(self, name: str) -> None:
        """切换底图数据源并刷新能力说明。"""
        self.basemap_source.set(name)
        self._set_tab_active("basemap_source", name)
        self._update_basemap_summary()

    def _choose_basemap_output(self) -> None:
        """选择底图输出目录。"""
        selected = filedialog.askdirectory(title="选择底图输出目录", initialdir=str(PROJECT_DIR))
        if selected:
            self.basemap_output.set(selected)

    def _basemap_bbox(self) -> tuple[float, float, float, float] | None:
        """返回底图使用的研究区范围；SHP 模式读取文件边界。"""
        if self.source_mode.get() == "bbox":
            return self._current_bbox()
        path_text = self.shp_path.get().strip()
        if not path_text:
            return None
        path = Path(path_text)
        if not path.exists():
            return None
        import download_buildings

        geometry = download_buildings.load_study_area(path, None)
        west, south, east, north = geometry.bounds
        return south, west, north, east

    def _update_basemap_summary(self, *_args: object) -> None:
        """更新底图页的中心点、覆盖半径与数据源能力。"""
        if not hasattr(self, "basemap_center_label"):
            return
        bbox = self._basemap_bbox()
        if bbox is None:
            self.basemap_center_label.configure(text="等待有效范围")
            self.basemap_radius_label.configure(text="—")
        else:
            center_lat, center_lon, radius = bbox_center_and_radius(bbox)
            self.basemap_center_label.configure(text=f"{center_lat:.5f} , {center_lon:.5f}")
            self.basemap_radius_label.configure(text=f"约 {radius:,} 米")
        info = self._basemap_infos.get(self.basemap_source.get())
        if info is None:
            return
        history = "支持历史版本" if info.supports_history else "仅当前影像"
        self.basemap_source_info.configure(text=f"{info.display_name} · {history} · zoom {info.min_zoom}–{info.max_zoom} · {info.attribution}")
        note = info.usage_note
        if info.experimental and not note:
            note = "实验性数据源，接口可能随时失效。"
        self.basemap_source_note.configure(text=note)
        self.basemap_date_hint.configure(text="日期范围仅对支持历史版本的数据源生效；留空表示不限制日期。" if info.supports_history else "当前数据源不支持历史版本，日期输入会被忽略，仅下载当前影像。")

    def _start_basemap_download(self) -> None:
        """校验底图参数并在后台线程中调用 TiffGetter。"""
        if self.running:
            return
        if self._basemap_error:
            messagebox.showerror("底图未配置", f"请安装 TiffGetter 及其依赖，详见 README。\n{self._basemap_error}", parent=self.master)
            return
        bbox = self._basemap_bbox()
        if bbox is None:
            messagebox.showerror("无法下载底图", "请先在“建筑轮廓”页填写有效范围或选择 SHP 研究区。", parent=self.master)
            return
        try:
            zoom = int(self.basemap_zoom.get().strip())
        except ValueError:
            messagebox.showerror("无法下载底图", "缩放级别必须是 1 到 20 的整数。", parent=self.master)
            return
        if not 1 <= zoom <= 20:
            messagebox.showerror("无法下载底图", "缩放级别必须是 1 到 20 的整数。", parent=self.master)
            return
        start = self.basemap_date_start.get().strip()
        end = self.basemap_date_end.get().strip()
        date_range: tuple[str, str] | None = None
        if start or end:
            if not (start and end):
                messagebox.showerror("无法下载底图", "日期范围需要同时填写开始与结束日期。", parent=self.master)
                return
            try:
                datetime.strptime(start, "%Y-%m-%d")
                datetime.strptime(end, "%Y-%m-%d")
            except ValueError:
                messagebox.showerror("无法下载底图", "日期格式应为 YYYY-MM-DD。", parent=self.master)
                return
            date_range = (start, end)
        output_dir = Path(self.basemap_output.get().strip() or BASEMAP_DEFAULT_OUTPUT)
        if not output_dir.is_absolute():
            output_dir = PROJECT_DIR / output_dir
        center_lat, center_lon, radius = bbox_center_and_radius(bbox)
        self.running = True
        self.download_button.configure(state="disabled")
        self.basemap_button.configure(state="disabled")
        self.basemap_status.configure(text="底图下载中…", text_color=OCHRE)
        self.basemap_progress.configure(mode="indeterminate")
        self.basemap_progress.start()
        self._write_log(f"开始下载底图：{self.basemap_source.get()} · zoom {zoom} · 中心 ({center_lat:.5f}, {center_lon:.5f}) · 半径 {radius} 米")
        threading.Thread(target=self._run_basemap, args=(self.basemap_source.get(), center_lat, center_lon, radius, zoom, date_range, output_dir), daemon=True).start()

    def _run_basemap(self, source: str, center_lat: float, center_lon: float, radius: int, zoom: int, date_range: tuple[str, str] | None, output_dir: Path) -> None:
        """在后台线程中调用 TiffGetter 下载影像底图，并把日志转发到界面。"""
        handler = QueueLogHandler(self.events)
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        targets = [logging.getLogger(), logging.getLogger("core"), logging.getLogger("TiffGetter"), logging.getLogger("providers")]
        previous_level = logging.getLogger().level
        for target in targets:
            target.addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        try:
            ImageFetcher, _infos = load_tiffgetter()
            fetcher = ImageFetcher(source=source)
            results = fetcher.fetch_all(lat=center_lat, lon=center_lon, zoom=zoom, output_dir=str(output_dir), buffer_size=radius, date_range=date_range, emit_events=False)
            success = sum(1 for result in results if result.get("status") == "success")
            self.events.put(("basemap_finished", (success, len(results) - success)))
        except Exception as error:
            self.events.put(("log", f"[底图] 下载失败：{error}"))
            self.events.put(("basemap_finished", (0, 0)))
        finally:
            for target in targets:
                target.removeHandler(handler)
            logging.getLogger().setLevel(previous_level)

    def _build_footer(self) -> None:
        """构建页脚。"""
        footer = ctk.CTkFrame(self, fg_color=PAPER)
        footer.grid(row=3, column=0, sticky="ew", padx=28, pady=(0, 8))
        tk.Frame(footer, height=1, bg=RULE).pack(fill="x")
        row = ctk.CTkFrame(footer, fg_color="transparent")
        row.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(row, text="数据 © OpenStreetMap contributors · ODbL", text_color=MUTED, font=self._mono_font(7)).pack(side="left")
        ctk.CTkLabel(row, text="底图 TiffGetter · Overpass API · v2.1.0", text_color=MUTED, font=self._mono_font(7)).pack(side="right")

    def _labeled_entry(self, parent: ctk.CTkFrame, label: str, variable: tk.StringVar, hint: str) -> None:
        """在弹窗中创建带标签的输入框。"""
        ctk.CTkLabel(parent, text=label, text_color=MUTED, font=self._mono_font(8)).pack(anchor="w", padx=16, pady=(0, 2))
        entry = ctk.CTkEntry(parent, textvariable=variable, placeholder_text=hint, fg_color=PAPER_LIGHT, border_color=RULE_STRONG, border_width=1, text_color=INK, corner_radius=2, height=34, font=self._mono_font(9))
        entry.pack(fill="x", padx=16, pady=(0, 8))
        entry.bind("<FocusIn>", lambda _event, widget=entry: widget.configure(border_color=OCHRE))
        entry.bind("<FocusOut>", lambda _event, widget=entry: widget.configure(border_color=RULE_STRONG))

    def _open_advanced_dialog(self) -> None:
        """打开高级选项弹窗，避免撑高主界面。"""
        if self.advanced_dialog is not None and self.advanced_dialog.winfo_exists():
            self.advanced_dialog.lift()
            return
        dialog = ctk.CTkToplevel(self.master)
        dialog.title("高级选项")
        dialog.geometry("480x470")
        dialog.resizable(False, False)
        dialog.configure(fg_color=PAPER)
        dialog.transient(self.master)
        self.advanced_dialog = dialog
        ctk.CTkLabel(dialog, text="细节设置", text_color=INK, font=self._display_font(20)).pack(anchor="w", padx=16, pady=(14, 2))
        ctk.CTkLabel(dialog, text="参数会立即应用到下一次下载。", text_color=MUTED, font=self._ui_font(9)).pack(anchor="w", padx=16, pady=(0, 8))
        self._labeled_entry(dialog, "网格大小（度）", self.tile_size, "0 表示不分块")
        self._labeled_entry(dialog, "每端点重试次数", self.retries, "默认 2")
        self._labeled_entry(dialog, "建筑类型（逗号分隔）", self.building_values, "留空表示全部")
        self._labeled_entry(dialog, "Overpass 端点（逗号分隔）", self.endpoints, "留空使用默认端点")
        ctk.CTkLabel(dialog, text="网格大小以度为单位；主界面会显示按中心纬度估算的米制尺寸。", text_color=MUTED, font=self._mono_font(7), wraplength=380, justify="left").pack(anchor="w", padx=16)
        ctk.CTkButton(dialog, text="完成", height=34, fg_color=INK, hover_color="#3A332A", text_color=PAPER_LIGHT, corner_radius=2, font=self._ui_font(10, "bold"), command=self._close_advanced_dialog).pack(fill="x", padx=16, pady=(12, 14))
        dialog.protocol("WM_DELETE_WINDOW", self._close_advanced_dialog)

    def _close_advanced_dialog(self) -> None:
        """关闭高级选项弹窗并释放引用。"""
        if self.advanced_dialog is not None:
            self.advanced_dialog.destroy()
            self.advanced_dialog = None

    def _update_advanced_summary(self, *_args: object) -> None:
        """更新主卡片上的高级参数摘要。"""
        if not hasattr(self, "advanced_summary"):
            return
        tile = self.tile_size.get().strip() or "0"
        retries = self.retries.get().strip() or "2"
        endpoint = "自定义端点" if self.endpoints.get().strip() else "端点默认"
        parts = [f"网格 {tile}°", f"重试 {retries}", endpoint]
        if self.building_values.get().strip():
            parts.append("已设类型")
        self.advanced_summary.configure(text=" · ".join(parts))

    def _update_tile_meter_hint(self, *_args: object) -> None:
        """按范围中心纬度更新网格对应的南北与东西米制尺寸。"""
        if not hasattr(self, "tile_meter_label"):
            return
        try:
            south, north = float(self.coordinates["南"].get()), float(self.coordinates["北"].get())
            if not -90 <= south <= 90 or not -90 <= north <= 90:
                raise ValueError
            north_south, east_west = tile_size_to_meters(self.tile_size.get(), (south + north) / 2)
            if north_south == 0:
                text = "0 表示不分块。"
            else:
                text = f"约 {north_south:,.0f} × {east_west:,.0f} 米 / 网格"
        except ValueError:
            text = "请输入有效范围和网格大小以换算米。"
        self.tile_meter_label.configure(text=text)

    def _select_source(self, value: str) -> None:
        """切换研究区输入模式，并同步底图覆盖信息。"""
        self.source_mode.set(value)
        self._set_tab_active("source", value)
        self._update_source_state()
        self._update_basemap_summary()

    def _select_format(self, value: str) -> None:
        self.output_format.set(value)
        self._change_output_format(value)
        self._set_tab_active("format", value)

    def _select_preset(self, value: str) -> None:
        self.snapshot_preset.set(value)
        self._set_tab_active("preset", value)
        self._apply_preset(value)

    def _current_bbox(self) -> tuple[float, float, float, float] | None:
        """把四个坐标输入解析为合法的 WGS84 范围，否则返回 None。"""
        try:
            south = float(self.coordinates["南"].get())
            west = float(self.coordinates["西"].get())
            north = float(self.coordinates["北"].get())
            east = float(self.coordinates["东"].get())
        except ValueError:
            return None
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            return None
        return south, west, north, east

    def _schedule_preview(self, *_args: object) -> None:
        """防抖地安排研究区预览重绘。"""
        if self._preview_job is not None:
            self.after_cancel(self._preview_job)
        self._preview_job = self.after(120, self._refresh_preview)

    def _refresh_preview(self) -> None:
        """重绘研究区预览。"""
        self._preview_job = None
        if hasattr(self, "preview_canvas"):
            _draw_aoi_preview(self.preview_canvas, self._current_bbox())

    def _update_source_state(self) -> None:
        is_bbox = self.source_mode.get() == "bbox"
        for entry in self.coordinate_entries:
            entry.configure(state="normal" if is_bbox else "disabled")
        self.shp_entry.configure(state="disabled" if is_bbox else "normal")
        self.shp_button.configure(state="normal" if is_bbox else "disabled")
        if "source" in self._tabs:
            self._set_tab_active("source", self.source_mode.get())

    def _apply_preset(self, preset: str) -> None:
        offsets = {"1 天前": 1, "7 天前": 7, "30 天前": 30, "90 天前": 90, "1 年前": 365}
        if preset in offsets:
            target = datetime.now(timezone.utc).date() - timedelta(days=offsets[preset])
            self.snapshot_date_text.set(target.strftime("%Y-%m-%d"))
            self.snapshot_time_text.set("00:00:00")
        self._update_snapshot_state()

    def _open_date_picker(self) -> None:
        try:
            selected = datetime.strptime(self.snapshot_date_text.get(), "%Y-%m-%d").date()
        except ValueError:
            selected = datetime.now(timezone.utc).date()
        self.calendar_month = date(selected.year, selected.month, 1)
        dialog = ctk.CTkToplevel(self.master)
        dialog.title("选择日期")
        dialog.geometry("380x380")
        dialog.resizable(False, False)
        dialog.configure(fg_color=PAPER)
        dialog.transient(self.master)
        dialog.grab_set()
        self._render_calendar(dialog)

    def _render_calendar(self, dialog: ctk.CTkToplevel) -> None:
        for child in dialog.winfo_children():
            child.destroy()
        header = ctk.CTkFrame(dialog, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(16, 10))
        ctk.CTkButton(header, text="«", width=42, height=34, fg_color=PAPER_LIGHT, border_width=1, border_color=RULE_STRONG, text_color=INK, hover_color="#EDE4D3", corner_radius=2, command=lambda: self._move_calendar_year(dialog, -1)).pack(side="left")
        ctk.CTkButton(header, text="‹", width=42, height=34, fg_color=PAPER_LIGHT, border_width=1, border_color=RULE_STRONG, text_color=INK, hover_color="#EDE4D3", corner_radius=2, command=lambda: self._move_calendar_month(dialog, -1)).pack(side="left", padx=(4, 0))
        ctk.CTkLabel(header, text=self.calendar_month.strftime("%Y 年 %m 月"), text_color=INK, font=self._ui_font(13, "bold")).pack(side="left", expand=True)
        ctk.CTkButton(header, text="›", width=42, height=34, fg_color=PAPER_LIGHT, border_width=1, border_color=RULE_STRONG, text_color=INK, hover_color="#EDE4D3", corner_radius=2, command=lambda: self._move_calendar_month(dialog, 1)).pack(side="right")
        ctk.CTkButton(header, text="»", width=42, height=34, fg_color=PAPER_LIGHT, border_width=1, border_color=RULE_STRONG, text_color=INK, hover_color="#EDE4D3", corner_radius=2, command=lambda: self._move_calendar_year(dialog, 1)).pack(side="right", padx=(0, 4))
        calendar = ctk.CTkFrame(dialog, fg_color="transparent")
        calendar.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        for column, weekday in enumerate(("一", "二", "三", "四", "五", "六", "日")):
            calendar.columnconfigure(column, weight=1)
            ctk.CTkLabel(calendar, text=weekday, text_color=MUTED, font=self._mono_font(8)).grid(row=0, column=column, pady=(0, 4))
        first_weekday, days = monthrange(self.calendar_month.year, self.calendar_month.month)
        for day_number in range(1, days + 1):
            row, column = divmod(first_weekday + day_number - 1, 7)
            selected = self.snapshot_date_text.get() == f"{self.calendar_month.year:04d}-{self.calendar_month.month:02d}-{day_number:02d}"
            ctk.CTkButton(calendar, text=str(day_number), width=42, height=32, corner_radius=2, fg_color=OCHRE if selected else PAPER_LIGHT, border_width=0 if selected else 1, border_color=RULE_STRONG, text_color=PAPER_LIGHT if selected else INK, hover_color=OCHRE_DARK if selected else "#EDE4D3", font=self._mono_font(9), command=lambda value=day_number: self._select_calendar_date(dialog, value)).grid(row=row + 1, column=column, padx=2, pady=2)

    def _move_calendar_month(self, dialog: ctk.CTkToplevel, offset: int) -> None:
        year, month = self.calendar_month.year, self.calendar_month.month + offset
        if month == 0:
            year, month = year - 1, 12
        elif month == 13:
            year, month = year + 1, 1
        self.calendar_month = date(year, month, 1)
        self._render_calendar(dialog)

    def _move_calendar_year(self, dialog: ctk.CTkToplevel, offset: int) -> None:
        self.calendar_month = date(self.calendar_month.year + offset, self.calendar_month.month, 1)
        self._render_calendar(dialog)

    def _select_calendar_date(self, dialog: ctk.CTkToplevel, day_number: int) -> None:
        self.snapshot_date_text.set(f"{self.calendar_month.year:04d}-{self.calendar_month.month:02d}-{day_number:02d}")
        dialog.destroy()

    def _update_snapshot_state(self) -> None:
        state = "disabled" if self.snapshot_preset.get() == "最新数据" else "normal"
        self.snapshot_date_entry.configure(state=state)
        self.snapshot_time_entry.configure(state=state)
        self.calendar_button.configure(state=state)
        if "preset" in self._tabs:
            self._set_tab_active("preset", self.snapshot_preset.get())
        if self.snapshot_preset.get() == "最新数据":
            self.snapshot_status.configure(text="UTC · 最新数据")
        else:
            self.snapshot_status.configure(text=f"UTC · {self.snapshot_date_text.get()} {self.snapshot_time_text.get()}")

    def _snapshot_value(self) -> str:
        if self.snapshot_preset.get() == "最新数据":
            return ""
        return snapshot_text_to_iso(self.snapshot_date_text.get(), self.snapshot_time_text.get())

    def _show_output_path_from_start(self, *_args: object) -> None:
        """Keep the filename field left-aligned after selecting a new location."""
        if hasattr(self, "output_entry"):
            self.after_idle(lambda: self.output_entry._entry.xview_moveto(0))

    def _change_output_format(self, output_format: str) -> None:
        path = Path(self.output_path.get() or "buildings.geojson")
        self.output_path.set(str(path.with_suffix(output_extension(output_format))))

    def _choose_shp(self) -> None:
        path = filedialog.askopenfilename(title="选择研究区 SHP 文件", filetypes=[("Shapefile", "*.shp")])
        if path:
            self.shp_path.set(path)
            if self.output_path.get().endswith(("buildings.geojson", "buildings.shp")):
                self.output_path.set(str(Path(path).with_name(f"{Path(path).stem}_buildings{output_extension(self.output_format.get())}")))

    def _choose_output(self) -> None:
        extension = output_extension(self.output_format.get())
        selected = filedialog.asksaveasfilename(title="保存建筑数据", defaultextension=extension, filetypes=[("GeoJSON", "*.geojson *.json"), ("Shapefile", "*.shp"), ("GeoPackage", "*.gpkg")])
        if selected:
            selected_path = Path(selected)
            self.output_path.set(str(selected_path.with_suffix(extension)))

    def _write_log(self, text: str) -> None:
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")

    def _start_download(self) -> None:
        if self.running:
            return
        try:
            validate_download_inputs(self.source_mode.get(), [self.coordinates[name].get() for name in ("南", "西", "北", "东")], self.shp_path.get(), self.output_path.get(), self.tile_size.get(), self.retries.get())
            command = build_command(self.source_mode.get(), [self.coordinates[name].get() for name in ("南", "西", "北", "东")], self.shp_path.get(), self.output_path.get(), self._snapshot_value(), self.tile_size.get(), self.retries.get(), self.building_values.get(), self.endpoints.get())
        except ValueError as error:
            messagebox.showerror("无法开始下载", str(error), parent=self.master)
            return
        destination = Path(self.output_path.get().strip())
        if not destination.is_absolute():
            destination = PROJECT_DIR / destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.running = True
        self.cancel_requested = False
        self.download_button.configure(state="disabled")
        self.basemap_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_label.configure(text="正在下载…")
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self._write_log("开始下载…下载期间可点击“取消下载”。")
        threading.Thread(target=self._run_download, args=(command,), daemon=True).start()

    def _cancel_download(self) -> None:
        if not self.running:
            return
        self.cancel_requested = True
        self.cancel_button.configure(state="disabled")
        self.status_label.configure(text="正在取消…")
        self._write_log("正在请求取消下载…")
        if self.process is not None:
            self.process.terminate()

    def _run_download(self, command: Sequence[str]) -> None:
        """后台运行下载子进程，逐行把输出放入事件队列并报告结束码。"""
        try:
            self.process = subprocess.Popen(command, cwd=PROJECT_DIR, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
            if self.cancel_requested:
                self.process.terminate()
            received_output = False
            assert self.process.stdout is not None
            for line in self.process.stdout:
                text = line.rstrip()
                if text:
                    received_output = True
                self.events.put(("log", text))
            returncode = self.process.wait()
            if not received_output:
                self.events.put(("log", "下载程序未输出日志。"))
            self.events.put(("finished", returncode or 0))
        except OSError as error:
            self.events.put(("log", f"无法启动下载程序：{error}"))
            self.events.put(("finished", 1))

    def _handle_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "log":
                    self._write_log(str(payload))
                elif event == "finished":
                    self.running = False
                    self.process = None
                    self.download_button.configure(state="normal")
                    self.basemap_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress.set(0)
                    if self.cancel_requested:
                        self.status_label.configure(text="已取消", text_color=MUTED)
                        self._write_log("下载已取消。")
                    elif payload == 0:
                        self.status_label.configure(text="下载完成", text_color=MOSS)
                        messagebox.showinfo("下载完成", f"建筑数据已保存至：\n{self.output_path.get()}", parent=self.master)
                    else:
                        self.status_label.configure(text="下载失败", text_color=ERROR)
                        messagebox.showerror("下载失败", "请查看运行日志，并检查网络、范围和参数。", parent=self.master)
                elif event == "basemap_finished":
                    success, failed = payload
                    self.running = False
                    self.download_button.configure(state="normal")
                    self.basemap_button.configure(state="normal")
                    self.basemap_progress.stop()
                    self.basemap_progress.configure(mode="determinate")
                    self.basemap_progress.set(0)
                    if success:
                        self.basemap_status.configure(text=f"底图完成 · 成功 {success} · 失败 {failed}", text_color=MOSS)
                        messagebox.showinfo("底图下载完成", f"已成功获取 {success} 个日期版本。\n输出目录：\n{self.basemap_output.get()}", parent=self.master)
                    else:
                        self.basemap_status.configure(text=f"底图失败 · 失败 {failed}", text_color=ERROR)
                        messagebox.showerror("底图下载失败", "请查看运行日志，并检查数据源、层级与日期范围。", parent=self.master)
        except queue.Empty:
            pass
        self.after(100, self._handle_events)


def main() -> None:
    """创建主窗口并启动事件循环。"""
    ctk.set_appearance_mode("light")
    root = ctk.CTk(fg_color=PAPER)
    DownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
