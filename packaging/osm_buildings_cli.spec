# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：把命令行入口打包为单文件 Windows 可执行程序。"""

from pathlib import Path

# SPECPATH 是 spec 文件所在目录（packaging），仓库根目录为其上一级
ROOT = Path(SPECPATH).resolve().parent

a = Analysis(
    [str(ROOT / "download_buildings.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[],
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
    name="osm-buildings-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
