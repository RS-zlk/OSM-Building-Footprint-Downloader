#!/usr/bin/env python3
"""Modern desktop interface for the OSM building footprint downloader."""

from __future__ import annotations

import queue
import math
import subprocess
import sys
import threading
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk


PROJECT_DIR = Path(__file__).resolve().parent
DOWNLOADER = PROJECT_DIR / "download_buildings.py"
PRESETS = ("最新数据", "自定义日期时间", "1 天前", "7 天前", "30 天前", "90 天前", "1 年前")
OUTPUT_FORMATS = ("GeoJSON (.geojson)", "Shapefile (.shp)")
BLACK = "#111111"
WHITE = "#FFFFFF"
PANEL = "#F7F7F7"
BORDER = "#E2E2E2"
MUTED = "#666666"
CONTROL = "#EEEEEE"
CONTROL_HOVER = "#DEDEDE"


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
    return ".shp" if output_format == "Shapefile (.shp)" else ".geojson"


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


def build_command(source_mode: str, bbox: Sequence[str] | str, shp_path: str, output_path: str, snapshot_date: str, tile_size: str, retries: str) -> list[str]:
    """Build a safe subprocess command from validated GUI field values."""
    command = [sys.executable, str(DOWNLOADER)]
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
    return command


class DownloaderApp(ctk.CTkFrame):
    def __init__(self, master: ctk.CTk) -> None:
        super().__init__(master, fg_color=WHITE, corner_radius=0)
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
        self.events: queue.Queue[tuple[str, str | int]] = queue.Queue()
        self.running = False
        self.cancel_requested = False
        self.process: subprocess.Popen[str] | None = None
        self.tile_size.trace_add("write", self._update_tile_meter_hint)
        self.coordinates["南"].trace_add("write", self._update_tile_meter_hint)
        self.coordinates["北"].trace_add("write", self._update_tile_meter_hint)
        self._build()
        self._update_source_state()
        self._update_tile_meter_hint()
        self.after(100, self._handle_events)

    def _build(self) -> None:
        self.master.title("OSM 建筑轮廓下载器")
        self.master.minsize(980, 720)
        self.master.configure(fg_color=WHITE)
        self.grid(sticky="nsew")
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ctk.CTkFrame(self, fg_color=WHITE)
        header.grid(row=0, column=0, sticky="ew", padx=34, pady=(28, 18))
        ctk.CTkLabel(header, text="OSM 建筑轮廓下载器", text_color=BLACK, font=ctk.CTkFont(size=25, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(header, text="选择范围、快照时间和输出格式，获取可直接用于 GIS 的建筑数据。", text_color=MUTED, font=ctk.CTkFont(size=13)).pack(anchor="w", pady=(4, 0))

        body = ctk.CTkFrame(self, fg_color=WHITE)
        body.grid(row=1, column=0, sticky="nsew", padx=34)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        self._build_query_panel(body)
        self._build_export_panel(body)
        self._build_log()

    def _card(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        return ctk.CTkFrame(parent, fg_color=PANEL, border_color=BORDER, border_width=1, corner_radius=14)

    def _section_title(self, parent: ctk.CTkFrame, number: str, title: str, subtitle: str) -> None:
        ctk.CTkLabel(parent, text=f"{number}  {title}", text_color=BLACK, font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", padx=18, pady=(17, 1))
        ctk.CTkLabel(parent, text=subtitle, text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", padx=18, pady=(0, 14))

    def _build_query_panel(self, parent: ctk.CTkFrame) -> None:
        panel = ctk.CTkFrame(parent, fg_color=WHITE)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        panel.columnconfigure(0, weight=1)

        extent = self._card(panel)
        extent.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self._section_title(extent, "01", "下载范围", "输入 WGS84 坐标，或选择一个 SHP 研究区。")
        radio_frame = ctk.CTkFrame(extent, fg_color="transparent")
        radio_frame.pack(fill="x", padx=18, pady=(0, 10))
        ctk.CTkRadioButton(radio_frame, text="经纬度范围", variable=self.source_mode, value="bbox", command=self._update_source_state, fg_color=BLACK, hover_color="#333333", text_color=BLACK).pack(side="left")
        ctk.CTkRadioButton(radio_frame, text="SHP 研究区", variable=self.source_mode, value="shp", command=self._update_source_state, fg_color=BLACK, hover_color="#333333", text_color=BLACK).pack(side="left", padx=22)
        coordinates = ctk.CTkFrame(extent, fg_color="transparent")
        coordinates.pack(fill="x", padx=18, pady=(0, 8))
        self.coordinate_entries: list[ctk.CTkEntry] = []
        for column, name in enumerate(("南", "西", "北", "东")):
            coordinates.columnconfigure(column, weight=1)
            field = ctk.CTkFrame(coordinates, fg_color="transparent")
            field.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0))
            ctk.CTkLabel(field, text=name, text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", pady=(0, 4))
            entry = ctk.CTkEntry(field, textvariable=self.coordinates[name], fg_color=WHITE, border_color=BORDER, text_color=BLACK, height=34)
            entry.pack(fill="x")
            self.coordinate_entries.append(entry)
        ctk.CTkLabel(extent, text="顺序已固定为：南、西、北、东。", text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", padx=18, pady=(0, 12))
        shp_row = ctk.CTkFrame(extent, fg_color="transparent")
        shp_row.pack(fill="x", padx=18, pady=(0, 18))
        self.shp_entry = ctk.CTkEntry(shp_row, textvariable=self.shp_path, placeholder_text="选择 .shp 研究区文件", fg_color=WHITE, border_color=BORDER, text_color=BLACK, height=34)
        self.shp_entry.pack(side="left", fill="x", expand=True)
        self.shp_button = ctk.CTkButton(shp_row, text="浏览", width=76, height=34, fg_color=WHITE, border_width=1, border_color=BLACK, text_color=BLACK, hover_color="#EDEDED", command=self._choose_shp)
        self.shp_button.pack(side="left", padx=(8, 0))

        snapshot = self._card(panel)
        snapshot.grid(row=1, column=0, sticky="ew")
        self._section_title(snapshot, "02", "历史快照", "所有日期和时间均按 UTC 解释。")
        preset = ctk.CTkOptionMenu(snapshot, variable=self.snapshot_preset, values=list(PRESETS), command=self._apply_preset, fg_color=CONTROL, button_color=CONTROL_HOVER, button_hover_color="#CFCFCF", text_color=BLACK, dropdown_fg_color=WHITE, dropdown_text_color=BLACK, height=34)
        preset.pack(anchor="w", padx=18, pady=(0, 12))
        date_frame = ctk.CTkFrame(snapshot, fg_color="transparent")
        date_frame.pack(fill="x", padx=18, pady=(0, 18))
        date_frame.columnconfigure(0, weight=3)
        date_frame.columnconfigure(1, weight=2)
        ctk.CTkLabel(date_frame, text="日期", text_color=MUTED, font=ctk.CTkFont(size=12)).grid(row=0, column=0, sticky="w", pady=(0, 4))
        ctk.CTkLabel(date_frame, text="时间（UTC）", text_color=MUTED, font=ctk.CTkFont(size=12)).grid(row=0, column=1, sticky="w", padx=(10, 0), pady=(0, 4))
        date_control = ctk.CTkFrame(date_frame, fg_color="transparent")
        date_control.grid(row=1, column=0, sticky="ew")
        date_control.columnconfigure(0, weight=1)
        self.snapshot_date_entry = ctk.CTkEntry(date_control, textvariable=self.snapshot_date_text, placeholder_text="YYYY-MM-DD", fg_color=WHITE, border_color=BORDER, text_color=BLACK, height=34)
        self.snapshot_date_entry.grid(row=0, column=0, sticky="ew")
        self.calendar_button = ctk.CTkButton(date_control, text="选择日期", width=86, height=34, fg_color=WHITE, border_width=1, border_color=BLACK, text_color=BLACK, hover_color="#EDEDED", command=self._open_date_picker)
        self.calendar_button.grid(row=0, column=1, padx=(6, 0))
        self.snapshot_time_entry = ctk.CTkEntry(date_frame, textvariable=self.snapshot_time_text, placeholder_text="HH:MM:SS", fg_color=WHITE, border_color=BORDER, text_color=BLACK, height=34)
        self.snapshot_time_entry.grid(row=1, column=1, sticky="ew", padx=(10, 0))
        self._update_snapshot_state()

    def _build_export_panel(self, parent: ctk.CTkFrame) -> None:
        panel = self._card(parent)
        panel.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        self._section_title(panel, "03", "导出设置", "选择格式和保存位置后开始下载。")
        ctk.CTkLabel(panel, text="保存文件类型", text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", padx=18, pady=(0, 4))
        format_menu = ctk.CTkOptionMenu(panel, variable=self.output_format, values=list(OUTPUT_FORMATS), command=self._change_output_format, fg_color=CONTROL, button_color=CONTROL_HOVER, button_hover_color="#CFCFCF", text_color=BLACK, dropdown_fg_color=WHITE, dropdown_text_color=BLACK, height=36)
        format_menu.pack(fill="x", padx=18, pady=(0, 14))
        ctk.CTkLabel(panel, text="输出位置", text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", padx=18, pady=(0, 4))
        self.output_entry = ctk.CTkEntry(panel, textvariable=self.output_path, fg_color=WHITE, border_color=BORDER, text_color=BLACK, height=36, font=ctk.CTkFont(size=11))
        self.output_entry.pack(fill="x", padx=18)
        self.output_entry._entry.configure(justify="left")
        self.output_path.trace_add("write", self._show_output_path_from_start)
        ctk.CTkButton(panel, text="选择保存位置", height=34, fg_color=WHITE, border_width=1, border_color=BLACK, text_color=BLACK, hover_color="#EDEDED", command=self._choose_output).pack(fill="x", padx=18, pady=(8, 16))
        ctk.CTkLabel(panel, text="高级选项", text_color=BLACK, font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=18, pady=(2, 9))
        self._labeled_entry(panel, "网格大小（度）", self.tile_size, "0 表示不分块")
        self.tile_meter_label = ctk.CTkLabel(panel, text="", text_color=MUTED, font=ctk.CTkFont(size=11), justify="left", wraplength=230)
        self.tile_meter_label.pack(anchor="w", padx=18, pady=(0, 10))
        self._labeled_entry(panel, "每端点重试次数", self.retries, "默认 2")
        self.download_button = ctk.CTkButton(panel, text="开始下载", height=42, fg_color=BLACK, hover_color="#333333", text_color=WHITE, font=ctk.CTkFont(size=14, weight="bold"), command=self._start_download)
        self.download_button.pack(fill="x", padx=18, pady=(22, 8))
        self.cancel_button = ctk.CTkButton(panel, text="取消下载", height=34, fg_color=WHITE, border_width=1, border_color=BLACK, text_color=BLACK, hover_color="#EDEDED", state="disabled", command=self._cancel_download)
        self.cancel_button.pack(fill="x", padx=18, pady=(0, 8))
        self.progress = ctk.CTkProgressBar(panel, mode="indeterminate", progress_color=BLACK, fg_color="#E5E5E5")
        self.progress.pack(fill="x", padx=18, pady=(0, 18))
        self.status_label = ctk.CTkLabel(panel, text="就绪", text_color=MUTED, font=ctk.CTkFont(size=12))
        self.status_label.pack(anchor="w", padx=18, pady=(0, 14))

    def _labeled_entry(self, parent: ctk.CTkFrame, label: str, variable: tk.StringVar, hint: str) -> None:
        ctk.CTkLabel(parent, text=label, text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", padx=18, pady=(0, 4))
        ctk.CTkEntry(parent, textvariable=variable, placeholder_text=hint, fg_color=WHITE, border_color=BORDER, text_color=BLACK, height=34).pack(fill="x", padx=18, pady=(0, 10))

    def _update_tile_meter_hint(self, *_args: object) -> None:
        """Show the user what a degree-based tile means at the selected latitude."""
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
                text = f"约 {north_south:,.0f} 米（南北）× {east_west:,.0f} 米（东西）\n按范围中心纬度估算"
        except ValueError:
            text = "请输入有效范围和网格大小以换算米。"
        self.tile_meter_label.configure(text=text)

    def _build_log(self) -> None:
        log_card = self._card(self)
        log_card.grid(row=2, column=0, sticky="nsew", padx=34, pady=(16, 28))
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(1, weight=1)
        ctk.CTkLabel(log_card, text="运行日志", text_color=BLACK, font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, sticky="w", padx=18, pady=(14, 8))
        self.log = ctk.CTkTextbox(log_card, height=145, fg_color=WHITE, border_color=BORDER, border_width=1, text_color=BLACK, corner_radius=8, font=ctk.CTkFont(size=12))
        self.log.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 14))
        self._write_log("就绪：请选择范围、快照和保存格式。")

    def _update_source_state(self) -> None:
        is_bbox = self.source_mode.get() == "bbox"
        for entry in self.coordinate_entries:
            entry.configure(state="normal" if is_bbox else "disabled")
        self.shp_entry.configure(state="disabled" if is_bbox else "normal")
        self.shp_button.configure(state="normal" if is_bbox else "disabled")

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
        dialog.geometry("330x330")
        dialog.resizable(False, False)
        dialog.transient(self.master)
        dialog.grab_set()
        self._render_calendar(dialog)

    def _render_calendar(self, dialog: ctk.CTkToplevel) -> None:
        for child in dialog.winfo_children():
            child.destroy()
        header = ctk.CTkFrame(dialog, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(16, 10))
        ctk.CTkButton(header, text="«", width=34, height=30, fg_color=WHITE, border_width=1, border_color=BORDER, text_color=BLACK, command=lambda: self._move_calendar_year(dialog, -1)).pack(side="left")
        ctk.CTkButton(header, text="‹", width=34, height=30, fg_color=WHITE, border_width=1, border_color=BORDER, text_color=BLACK, command=lambda: self._move_calendar_month(dialog, -1)).pack(side="left", padx=(4, 0))
        ctk.CTkLabel(header, text=self.calendar_month.strftime("%Y 年 %m 月"), text_color=BLACK, font=ctk.CTkFont(size=15, weight="bold")).pack(side="left", expand=True)
        ctk.CTkButton(header, text="›", width=34, height=30, fg_color=WHITE, border_width=1, border_color=BORDER, text_color=BLACK, command=lambda: self._move_calendar_month(dialog, 1)).pack(side="right")
        ctk.CTkButton(header, text="»", width=34, height=30, fg_color=WHITE, border_width=1, border_color=BORDER, text_color=BLACK, command=lambda: self._move_calendar_year(dialog, 1)).pack(side="right", padx=(0, 4))
        calendar = ctk.CTkFrame(dialog, fg_color="transparent")
        calendar.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        for column, weekday in enumerate(("一", "二", "三", "四", "五", "六", "日")):
            calendar.columnconfigure(column, weight=1)
            ctk.CTkLabel(calendar, text=weekday, text_color=MUTED, font=ctk.CTkFont(size=12)).grid(row=0, column=column, pady=(0, 4))
        first_weekday, days = monthrange(self.calendar_month.year, self.calendar_month.month)
        for day_number in range(1, days + 1):
            row, column = divmod(first_weekday + day_number - 1, 7)
            selected = self.snapshot_date_text.get() == f"{self.calendar_month.year:04d}-{self.calendar_month.month:02d}-{day_number:02d}"
            ctk.CTkButton(calendar, text=str(day_number), width=34, height=28, corner_radius=6, fg_color=BLACK if selected else WHITE, border_width=0 if selected else 1, border_color=BORDER, text_color=WHITE if selected else BLACK, hover_color="#333333" if selected else "#EDEDED", command=lambda value=day_number: self._select_calendar_date(dialog, value)).grid(row=row + 1, column=column, padx=2, pady=2)

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
        selected = filedialog.asksaveasfilename(title="保存建筑数据", defaultextension=extension, filetypes=[("GeoJSON", "*.geojson *.json"), ("Shapefile", "*.shp")])
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
            command = build_command(self.source_mode.get(), [self.coordinates[name].get() for name in ("南", "西", "北", "东")], self.shp_path.get(), self.output_path.get(), self._snapshot_value(), self.tile_size.get(), self.retries.get())
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
        self.cancel_button.configure(state="normal")
        self.status_label.configure(text="正在下载…")
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
                    self.cancel_button.configure(state="disabled")
                    self.progress.stop()
                    if self.cancel_requested:
                        self.status_label.configure(text="已取消")
                        self._write_log("下载已取消。")
                    elif payload == 0:
                        self.status_label.configure(text="下载完成")
                        messagebox.showinfo("下载完成", f"建筑数据已保存至：\n{self.output_path.get()}", parent=self.master)
                    else:
                        self.status_label.configure(text="下载失败")
                        messagebox.showerror("下载失败", "请查看运行日志，并检查网络、范围和参数。", parent=self.master)
        except queue.Empty:
            pass
        self.after(100, self._handle_events)


def main() -> None:
    ctk.set_appearance_mode("light")
    root = ctk.CTk(fg_color=WHITE)
    DownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
