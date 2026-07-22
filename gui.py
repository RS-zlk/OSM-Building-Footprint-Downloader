#!/usr/bin/env python3
"""Modern desktop interface for the OSM building footprint downloader."""

from __future__ import annotations

import queue
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


def snapshot_to_iso(day: date, hour: str, minute: str, second: str) -> str:
    """Return a validated UTC timestamp from structured date/time controls."""
    try:
        value = datetime(day.year, day.month, day.day, int(hour), int(minute), int(second), tzinfo=timezone.utc)
    except ValueError as error:
        raise ValueError("历史快照时间无效。") from error
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def available_days(year: str, month: str) -> list[str]:
    return [f"{value:02d}" for value in range(1, monthrange(int(year), int(month))[1] + 1)]


def output_extension(output_format: str) -> str:
    return ".shp" if output_format == "Shapefile (.shp)" else ".geojson"


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
        self.output_path = tk.StringVar(value=str(PROJECT_DIR / "data" / "buildings.geojson"))
        self.output_format = tk.StringVar(value=OUTPUT_FORMATS[0])
        self.snapshot_preset = tk.StringVar(value="最新数据")
        self.snapshot_year = tk.StringVar(value=str(now.year))
        self.snapshot_month = tk.StringVar(value=f"{now.month:02d}")
        self.snapshot_day = tk.StringVar(value=f"{now.day:02d}")
        self.snapshot_hour = tk.StringVar(value="00")
        self.snapshot_minute = tk.StringVar(value="00")
        self.snapshot_second = tk.StringVar(value="00")
        self.tile_size = tk.StringVar(value="0.05")
        self.retries = tk.StringVar(value="2")
        self.events: queue.Queue[tuple[str, str | int]] = queue.Queue()
        self.running = False
        self.snapshot_year.trace_add("write", self._update_days)
        self.snapshot_month.trace_add("write", self._update_days)
        self._build()
        self.after(100, self._handle_events)

    def _build(self) -> None:
        self.master.title("OSM 建筑矢量下载器")
        self.master.minsize(980, 720)
        self.master.configure(fg_color=WHITE)
        self.grid(sticky="nsew")
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ctk.CTkFrame(self, fg_color=WHITE)
        header.grid(row=0, column=0, sticky="ew", padx=34, pady=(28, 18))
        ctk.CTkLabel(header, text="OSM 建筑矢量下载器", text_color=BLACK, font=ctk.CTkFont(size=25, weight="bold")).pack(anchor="w")
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
        preset = ctk.CTkOptionMenu(snapshot, variable=self.snapshot_preset, values=list(PRESETS), command=self._apply_preset, fg_color=WHITE, button_color=BLACK, button_hover_color="#333333", text_color=BLACK, dropdown_fg_color=WHITE, dropdown_text_color=BLACK, height=34)
        preset.pack(anchor="w", padx=18, pady=(0, 12))
        date_frame = ctk.CTkFrame(snapshot, fg_color="transparent")
        date_frame.pack(fill="x", padx=18, pady=(0, 18))
        self.date_time_boxes: list[ctk.CTkOptionMenu] = []
        current_year = datetime.now(timezone.utc).year
        controls = (("年", self.snapshot_year, [str(value) for value in range(current_year, 2003, -1)]), ("月", self.snapshot_month, [f"{value:02d}" for value in range(1, 13)]), ("日", self.snapshot_day, [f"{value:02d}" for value in range(1, 32)]), ("时", self.snapshot_hour, [f"{value:02d}" for value in range(24)]), ("分", self.snapshot_minute, [f"{value:02d}" for value in range(60)]), ("秒", self.snapshot_second, [f"{value:02d}" for value in range(60)]))
        for index, (label, variable, values) in enumerate(controls):
            field = ctk.CTkFrame(date_frame, fg_color="transparent")
            field.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 6, 0))
            ctk.CTkLabel(field, text=label, text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", pady=(0, 4))
            menu = ctk.CTkOptionMenu(field, variable=variable, values=values, fg_color=WHITE, button_color=BLACK, button_hover_color="#333333", text_color=BLACK, dropdown_fg_color=WHITE, dropdown_text_color=BLACK, height=34, width=70)
            menu.pack(fill="x")
            self.date_time_boxes.append(menu)
        self._update_snapshot_state()

    def _build_export_panel(self, parent: ctk.CTkFrame) -> None:
        panel = self._card(parent)
        panel.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        self._section_title(panel, "03", "导出设置", "选择格式和保存位置后开始下载。")
        ctk.CTkLabel(panel, text="保存文件类型", text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", padx=18, pady=(0, 4))
        format_menu = ctk.CTkOptionMenu(panel, variable=self.output_format, values=list(OUTPUT_FORMATS), command=self._change_output_format, fg_color=WHITE, button_color=BLACK, button_hover_color="#333333", text_color=BLACK, dropdown_fg_color=WHITE, dropdown_text_color=BLACK, height=36)
        format_menu.pack(fill="x", padx=18, pady=(0, 14))
        ctk.CTkLabel(panel, text="输出位置", text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", padx=18, pady=(0, 4))
        self.output_entry = ctk.CTkEntry(panel, textvariable=self.output_path, fg_color=WHITE, border_color=BORDER, text_color=BLACK, height=36)
        self.output_entry.pack(fill="x", padx=18)
        ctk.CTkButton(panel, text="选择保存位置", height=34, fg_color=WHITE, border_width=1, border_color=BLACK, text_color=BLACK, hover_color="#EDEDED", command=self._choose_output).pack(fill="x", padx=18, pady=(8, 16))
        ctk.CTkLabel(panel, text="高级选项", text_color=BLACK, font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=18, pady=(2, 9))
        self._labeled_entry(panel, "网格大小（度）", self.tile_size, "0 表示不分块")
        self._labeled_entry(panel, "每端点重试次数", self.retries, "默认 2")
        self.download_button = ctk.CTkButton(panel, text="开始下载", height=42, fg_color=BLACK, hover_color="#333333", text_color=WHITE, font=ctk.CTkFont(size=14, weight="bold"), command=self._start_download)
        self.download_button.pack(fill="x", padx=18, pady=(22, 8))
        self.progress = ctk.CTkProgressBar(panel, mode="indeterminate", progress_color=BLACK, fg_color="#E5E5E5")
        self.progress.pack(fill="x", padx=18, pady=(0, 18))

    def _labeled_entry(self, parent: ctk.CTkFrame, label: str, variable: tk.StringVar, hint: str) -> None:
        ctk.CTkLabel(parent, text=label, text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", padx=18, pady=(0, 4))
        ctk.CTkEntry(parent, textvariable=variable, placeholder_text=hint, fg_color=WHITE, border_color=BORDER, text_color=BLACK, height=34).pack(fill="x", padx=18, pady=(0, 10))

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
            self.snapshot_year.set(str(target.year))
            self.snapshot_month.set(f"{target.month:02d}")
            self.snapshot_day.set(f"{target.day:02d}")
            self.snapshot_hour.set("00")
            self.snapshot_minute.set("00")
            self.snapshot_second.set("00")
        self._update_snapshot_state()

    def _update_days(self, *_args: object) -> None:
        try:
            values = available_days(self.snapshot_year.get(), self.snapshot_month.get())
        except ValueError:
            return
        if hasattr(self, "date_time_boxes"):
            self.date_time_boxes[2].configure(values=values)
        if self.snapshot_day.get() not in values:
            self.snapshot_day.set(values[-1])

    def _update_snapshot_state(self) -> None:
        state = "disabled" if self.snapshot_preset.get() == "最新数据" else "normal"
        for menu in self.date_time_boxes:
            menu.configure(state=state)

    def _snapshot_value(self) -> str:
        if self.snapshot_preset.get() == "最新数据":
            return ""
        selected_date = date(int(self.snapshot_year.get()), int(self.snapshot_month.get()), int(self.snapshot_day.get()))
        return snapshot_to_iso(selected_date, self.snapshot_hour.get(), self.snapshot_minute.get(), self.snapshot_second.get())

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
            command = build_command(self.source_mode.get(), [self.coordinates[name].get() for name in ("南", "西", "北", "东")], self.shp_path.get(), self.output_path.get(), self._snapshot_value(), self.tile_size.get(), self.retries.get())
        except ValueError as error:
            messagebox.showerror("无法开始下载", str(error), parent=self.master)
            return
        self.running = True
        self.download_button.configure(state="disabled")
        self.progress.start()
        self._write_log("开始下载…")
        threading.Thread(target=self._run_download, args=(command,), daemon=True).start()

    def _run_download(self, command: Sequence[str]) -> None:
        try:
            completed = subprocess.run(command, cwd=PROJECT_DIR, text=True, capture_output=True, check=False)
            self.events.put(("log", (completed.stdout + completed.stderr).strip() or "下载程序未输出日志。"))
            self.events.put(("finished", completed.returncode))
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
                    self.download_button.configure(state="normal")
                    self.progress.stop()
                    if payload == 0:
                        messagebox.showinfo("下载完成", f"建筑数据已保存至：\n{self.output_path.get()}", parent=self.master)
                    else:
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
