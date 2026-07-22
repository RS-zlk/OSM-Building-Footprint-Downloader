#!/usr/bin/env python3
"""Accessible desktop interface for the OSM building footprint downloader."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from tkcalendar import DateEntry


PROJECT_DIR = Path(__file__).resolve().parent
DOWNLOADER = PROJECT_DIR / "download_buildings.py"
PRESETS = ("最新数据", "自定义日期时间", "1 天前", "7 天前", "30 天前", "90 天前", "1 年前")


def snapshot_to_iso(day: date, hour: str, minute: str, second: str) -> str:
    """Return a validated UTC timestamp from structured date/time controls."""
    try:
        value = datetime(day.year, day.month, day.day, int(hour), int(minute), int(second), tzinfo=timezone.utc)
    except ValueError as error:
        raise ValueError("历史快照时间无效。") from error
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_command(
    source_mode: str,
    bbox: Sequence[str] | str,
    shp_path: str,
    output_path: str,
    snapshot_date: str,
    tile_size: str,
    retries: str,
) -> list[str]:
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
        raise ValueError("请选择输出 GeoJSON 文件。")
    command.extend(["--output", output_path.strip()])
    if snapshot_date:
        command.extend(["--date", snapshot_date])
    if tile_size.strip():
        command.extend(["--tile-size", tile_size.strip()])
    if retries.strip():
        command.extend(["--retries", retries.strip()])
    return command


class DownloaderApp(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=20)
        self.master = master
        self.source_mode = tk.StringVar(value="bbox")
        self.coordinates = {name: tk.StringVar(value=value) for name, value in (("南", "30.742"), ("西", "120.480"), ("北", "30.750"), ("东", "120.490"))}
        self.shp_path = tk.StringVar()
        self.output_path = tk.StringVar(value=str(PROJECT_DIR / "data" / "buildings.geojson"))
        self.snapshot_preset = tk.StringVar(value="最新数据")
        self.snapshot_hour = tk.StringVar(value="00")
        self.snapshot_minute = tk.StringVar(value="00")
        self.snapshot_second = tk.StringVar(value="00")
        self.tile_size = tk.StringVar(value="0.05")
        self.retries = tk.StringVar(value="2")
        self.events: queue.Queue[tuple[str, str | int]] = queue.Queue()
        self.running = False
        self._configure_style()
        self._build()
        self.after(100, self._handle_events)

    def _configure_style(self) -> None:
        style = ttk.Style(self.master)
        style.theme_use("clam")
        style.configure("Title.TLabel", font=("TkDefaultFont", 19, "bold"), foreground="#12324a")
        style.configure("Subtitle.TLabel", foreground="#526777")
        style.configure("Card.TLabelframe", padding=14)
        style.configure("Card.TLabelframe.Label", font=("TkDefaultFont", 11, "bold"), foreground="#12324a")
        style.configure("Accent.TButton", padding=(14, 7), font=("TkDefaultFont", 10, "bold"))

    def _build(self) -> None:
        self.master.title("OSM 建筑矢量下载器")
        self.master.minsize(820, 700)
        self.grid(sticky="nsew")
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        ttk.Label(header, text="OSM 建筑矢量下载器", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="下载当前或历史建筑轮廓，支持研究区 SHP、严格裁剪和自动重试。", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))

        self._build_extent_card()
        self._build_options_card()
        self._build_output_card()
        self._build_log()

    def _build_extent_card(self) -> None:
        card = ttk.LabelFrame(self, text="1. 选择下载范围", style="Card.TLabelframe")
        card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        card.columnconfigure(1, weight=1)
        ttk.Radiobutton(card, text="输入经纬度范围", variable=self.source_mode, value="bbox", command=self._update_source_state).grid(row=0, column=0, sticky="w")
        coordinate_frame = ttk.Frame(card)
        coordinate_frame.grid(row=0, column=1, sticky="ew", padx=(16, 0))
        self.coordinate_entries: list[ttk.Entry] = []
        for column, name in enumerate(("南", "西", "北", "东")):
            ttk.Label(coordinate_frame, text=name).grid(row=0, column=column * 2, sticky="w")
            entry = ttk.Entry(coordinate_frame, textvariable=self.coordinates[name], width=13)
            entry.grid(row=0, column=column * 2 + 1, padx=(4, 12), sticky="ew")
            self.coordinate_entries.append(entry)
        ttk.Label(card, text="WGS84 / EPSG:4326；南 < 北，西 < 东。", style="Subtitle.TLabel").grid(row=1, column=1, sticky="w", padx=(16, 0), pady=(4, 0))

        ttk.Radiobutton(card, text="选择 SHP 研究区", variable=self.source_mode, value="shp", command=self._update_source_state).grid(row=2, column=0, sticky="w", pady=(12, 0))
        self.shp_entry = ttk.Entry(card, textvariable=self.shp_path)
        self.shp_entry.grid(row=2, column=1, sticky="ew", padx=(16, 8), pady=(12, 0))
        self.shp_button = ttk.Button(card, text="浏览…", command=self._choose_shp)
        self.shp_button.grid(row=2, column=2, sticky="e", pady=(12, 0))
        ttk.Label(card, text="将自动读取 .prj 并严格裁剪建筑轮廓。", style="Subtitle.TLabel").grid(row=3, column=1, sticky="w", padx=(16, 0), pady=(4, 0))

    def _build_options_card(self) -> None:
        card = ttk.LabelFrame(self, text="2. 选择数据时间与下载选项", style="Card.TLabelframe")
        card.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text="数据快照").grid(row=0, column=0, sticky="w")
        self.preset_box = ttk.Combobox(card, textvariable=self.snapshot_preset, values=PRESETS, state="readonly", width=16)
        self.preset_box.grid(row=0, column=1, sticky="w", padx=(16, 10))
        self.preset_box.bind("<<ComboboxSelected>>", self._apply_preset)

        ttk.Label(card, text="日期（UTC）").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.date_picker = DateEntry(card, date_pattern="y-mm-dd", width=13)
        self.date_picker.grid(row=1, column=1, sticky="w", padx=(16, 10), pady=(10, 0))
        time_frame = ttk.Frame(card)
        time_frame.grid(row=1, column=2, sticky="w", pady=(10, 0))
        self.time_boxes = []
        for variable, values, width in ((self.snapshot_hour, [f"{value:02d}" for value in range(24)], 3), (self.snapshot_minute, [f"{value:02d}" for value in range(60)], 3), (self.snapshot_second, [f"{value:02d}" for value in range(60)], 3)):
            box_widget = ttk.Combobox(time_frame, textvariable=variable, values=values, state="readonly", width=width)
            box_widget.pack(side="left")
            self.time_boxes.append(box_widget)
            if variable is not self.snapshot_second:
                ttk.Label(time_frame, text=":").pack(side="left", padx=2)
        ttk.Label(card, text="选择“最新数据”时不会发送历史日期。", style="Subtitle.TLabel").grid(row=2, column=1, columnspan=2, sticky="w", padx=(16, 0), pady=(4, 0))

        ttk.Separator(card).grid(row=3, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(card, text="网格大小（度）").grid(row=4, column=0, sticky="w")
        ttk.Entry(card, textvariable=self.tile_size, width=16).grid(row=4, column=1, sticky="w", padx=(16, 10))
        ttk.Label(card, text="0 表示不分块；推荐保留 0.05。", style="Subtitle.TLabel").grid(row=4, column=2, sticky="w")
        ttk.Label(card, text="每端点重试次数").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(card, textvariable=self.retries, width=16).grid(row=5, column=1, sticky="w", padx=(16, 10), pady=(8, 0))
        ttk.Label(card, text="网络波动时会自动切换备用服务。", style="Subtitle.TLabel").grid(row=5, column=2, sticky="w", pady=(8, 0))
        self._update_snapshot_state()

    def _build_output_card(self) -> None:
        card = ttk.LabelFrame(self, text="3. 保存结果", style="Card.TLabelframe")
        card.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        card.columnconfigure(0, weight=1)
        ttk.Entry(card, textvariable=self.output_path).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(card, text="选择位置…", command=self._choose_output).grid(row=0, column=1)
        controls = ttk.Frame(card)
        controls.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.download_button = ttk.Button(controls, text="开始下载", style="Accent.TButton", command=self._start_download)
        self.download_button.pack(side="left")
        ttk.Button(controls, text="清空日志", command=self._clear_log).pack(side="left", padx=8)
        self.progress = ttk.Progressbar(controls, mode="indeterminate", length=180)
        self.progress.pack(side="right")

    def _build_log(self) -> None:
        card = ttk.LabelFrame(self, text="运行日志", style="Card.TLabelframe")
        card.grid(row=4, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(0, weight=1)
        self.log = tk.Text(card, height=9, wrap="word", state="disabled", relief="flat", background="#f7f9fb")
        scrollbar = ttk.Scrollbar(card, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._write_log("就绪：请选择范围、快照时间与输出位置。")

    def _update_source_state(self) -> None:
        is_bbox = self.source_mode.get() == "bbox"
        for entry in self.coordinate_entries:
            entry.configure(state="normal" if is_bbox else "disabled")
        self.shp_entry.configure(state="disabled" if is_bbox else "normal")
        self.shp_button.configure(state="disabled" if is_bbox else "normal")

    def _apply_preset(self, _event: object = None) -> None:
        preset = self.snapshot_preset.get()
        offsets = {"1 天前": 1, "7 天前": 7, "30 天前": 30, "90 天前": 90, "1 年前": 365}
        if preset in offsets:
            self.date_picker.set_date(datetime.now(timezone.utc).date() - timedelta(days=offsets[preset]))
            self.snapshot_hour.set("00")
            self.snapshot_minute.set("00")
            self.snapshot_second.set("00")
        self._update_snapshot_state()

    def _update_snapshot_state(self) -> None:
        enabled = self.snapshot_preset.get() != "最新数据"
        self.date_picker.configure(state="normal" if enabled else "disabled")
        for box_widget in self.time_boxes:
            box_widget.configure(state="readonly" if enabled else "disabled")

    def _snapshot_value(self) -> str:
        if self.snapshot_preset.get() == "最新数据":
            return ""
        return snapshot_to_iso(self.date_picker.get_date(), self.snapshot_hour.get(), self.snapshot_minute.get(), self.snapshot_second.get())

    def _choose_shp(self) -> None:
        path = filedialog.askopenfilename(title="选择研究区 SHP 文件", filetypes=[("Shapefile", "*.shp")])
        if path:
            self.shp_path.set(path)
            if not self.output_path.get() or self.output_path.get().endswith("buildings.geojson"):
                self.output_path.set(str(Path(path).with_name(f"{Path(path).stem}_buildings.geojson")))

    def _choose_output(self) -> None:
        path = filedialog.asksaveasfilename(title="保存 GeoJSON", defaultextension=".geojson", filetypes=[("GeoJSON", "*.geojson *.json")])
        if path:
            self.output_path.set(path)

    def _write_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

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
        self.progress.start(10)
        self._write_log("开始下载…")
        threading.Thread(target=self._run_download, args=(command,), daemon=True).start()

    def _run_download(self, command: Sequence[str]) -> None:
        try:
            completed = subprocess.run(command, cwd=PROJECT_DIR, text=True, capture_output=True, check=False)
            output = (completed.stdout + completed.stderr).strip()
            self.events.put(("log", output or "下载程序未输出日志。"))
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
                        messagebox.showerror("下载失败", "请查看下方日志，并检查网络、范围和参数。", parent=self.master)
        except queue.Empty:
            pass
        self.after(100, self._handle_events)


def main() -> None:
    root = tk.Tk()
    DownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
