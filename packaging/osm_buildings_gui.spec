# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：把桌面界面打包为单文件 Windows 可执行程序。"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

# SPECPATH 是 spec 文件所在目录（packaging），仓库根目录为其上一级
ROOT = Path(SPECPATH).resolve().parent
# TiffGetter 源码目录：底图下载功能在运行期导入其 providers 与 core 包
TIFFGETTER = Path(os.environ.get("TIFFGETTER_HOME", str(ROOT.parent / "TiffGetter"))).resolve()

a = Analysis(
    [str(ROOT / "gui.py")],
    pathex=[str(ROOT), str(TIFFGETTER)],
    binaries=[],
    datas=collect_data_files("customtkinter"),
    hiddenimports=[
        "satellite_processor",
        "core",
        "core.dedup",
        "core.errors",
        "core.events",
        "core.fetch",
        "core.geo",
        "core.release",
        "providers",
        "providers.base",
        "providers.bing",
        "providers.esri_current",
        "providers.google_earth",
        "providers.google_earth_client",
        "providers.osm",
        "providers.registry",
        "providers.wayback",
        "providers.xyz",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="osm-buildings-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
