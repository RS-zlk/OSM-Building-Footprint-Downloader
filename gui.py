#!/usr/bin/env python3
"""Desktop interface for the OSM building footprint downloader."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


PROJECT_DIR = Path(__file__).resolve().parent
DOWNLOADER = PROJECT_DIR / "download_buildings.py"


def build_command(
    source_mode: str,
    bbox: str,
    shp_path: str,
    output_path: str,
    snapshot_date: str,
    tile_size: str,
    retries: str,
) -> list[str]:
    """Build a safe subprocess command from the GUI field values."""
    command = [sys.executable, str(DOWNLOADER)]
    if source_mode == "shp":
        if not shp_path.strip():
            raise ValueError("请选择研究区 SHP 文件。")
        command.extend(["--shp", shp_path.strip()])
    else:
        values = bbox.replace(",", " ").split()
        if len(values) != 4:
            raise ValueError("范围必须包含四个数值，顺序为：南 西 北 东。")
        command.extend(["--bbox", *values])
    if not output_path.strip():
        raise ValueError("请选择输出 GeoJSON 文件。")
    command.extend(["--output", output_path.strip()])
    if snapshot_date.strip():
        command.extend(["--date", snapshot_date.strip()])
    if tile_size.strip():
        command.extend(["--tile-size", tile_size.strip()])
    if retries.strip():
        command.extend(["--retries", retries.strip()])
    return command


class DownloaderApp(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=16)
        self.master = master
        self.source_mode = tk.StringVar(value="bbox")
        self.bbox = tk.StringVar(value="30.742 120.480 30.750 120.490")
        self.shp_path = tk.StringVar()
        self.output_path = tk.StringVar(value=str(PROJECT_DIR / "data" / "buildings.geojson"))
        self.snapshot_date = tk.StringVar()
        self.tile_size = tk.StringVar(value="0.05")
        self.retries = tk.StringVar(value="2")
        self.events: queue.Queue[tuple[str, str | int]] = queue.Queue()
        self.running = False
        self._build()
        self.after(100, self._handle_events)

    def _build(self) -> None:
        self.master.title("OSM 建筑矢量下载器")
        self.master.minsize(720, 560)
        self.grid(sticky="nsew")
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(7, weight=1)

        ttk.Label(self, text="下载范围", font=("TkDefaultFont", 13, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Radiobutton(self, text="经纬度范围", variable=self.source_mode, value="bbox", command=self._update_source_state).grid(row=1, column=0, sticky="w")
        self.bbox_entry = ttk.Entry(self, textvariable=self.bbox)
        self.bbox_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        ttk.Label(self, text="顺序：南 西 北 东（WGS84）").grid(row=2, column=1, columnspan=2, sticky="w", padx=(8, 0))

        ttk.Radiobutton(self, text="SHP 研究区", variable=self.source_mode, value="shp", command=self._update_source_state).grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.shp_entry = ttk.Entry(self, textvariable=self.shp_path)
        self.shp_entry.grid(row=3, column=1, sticky="ew", padx=(8, 8), pady=(8, 0))
        self.shp_button = ttk.Button(self, text="选择…", command=self._choose_shp)
        self.shp_button.grid(row=3, column=2, sticky="e", pady=(8, 0))

        ttk.Separator(self).grid(row=4, column=0, columnspan=3, sticky="ew", pady=14)
        ttk.Label(self, text="下载选项", font=("TkDefaultFont", 13, "bold")).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._labeled_entry("历史快照（可选，UTC）", self.snapshot_date, 6, "例如 2024-01-01T00:00:00Z")
        self._labeled_entry("网格大小（度）", self.tile_size, 7, "0 表示不分块")
        self._labeled_entry("每端点重试次数", self.retries, 8, "默认 2")

        ttk.Label(self, text="输出 GeoJSON").grid(row=9, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(self, textvariable=self.output_path).grid(row=9, column=1, sticky="ew", padx=(8, 8), pady=(10, 0))
        ttk.Button(self, text="保存到…", command=self._choose_output).grid(row=9, column=2, sticky="e", pady=(10, 0))

        buttons = ttk.Frame(self)
        buttons.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(14, 8))
        self.download_button = ttk.Button(buttons, text="开始下载", command=self._start_download)
        self.download_button.pack(side="left")
        ttk.Button(buttons, text="清空日志", command=self._clear_log).pack(side="left", padx=8)
        self.progress = ttk.Progressbar(buttons, mode="indeterminate", length=180)
        self.progress.pack(side="right")

        self.log = tk.Text(self, height=12, wrap="word", state="disabled")
        self.log.grid(row=11, column=0, columnspan=3, sticky="nsew")
        self.rowconfigure(11, weight=1)
        self._update_source_state()

    def _labeled_entry(self, label: str, variable: tk.StringVar, row: int, hint: str) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=(8, 8), pady=3)
        ttk.Label(self, text=hint).grid(row=row, column=2, sticky="w", pady=3)

    def _update_source_state(self) -> None:
        is_bbox = self.source_mode.get() == "bbox"
        self.bbox_entry.configure(state="normal" if is_bbox else "disabled")
        self.shp_entry.configure(state="disabled" if is_bbox else "normal")
        self.shp_button.configure(state="disabled" if is_bbox else "normal")

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
            command = build_command(self.source_mode.get(), self.bbox.get(), self.shp_path.get(), self.output_path.get(), self.snapshot_date.get(), self.tile_size.get(), self.retries.get())
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
