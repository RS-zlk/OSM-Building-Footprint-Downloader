# 更新日志

本文件记录本仓库（fork）的变更，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [2.1.0] - 开发中

### 变更

- 建立 fork 二次开发基线，补充治理与上游同步说明。
- GUI 输出位置默认改为 `data/buildings.geojson`，并保持路径显示左对齐。
- 下载失败时输出可读的失败原因，便于定位网络、参数或输出问题。
- GUI 实时显示下载子进程日志，支持取消下载并报告结束状态。
- 新增 `--deadline` 总时限与 Retry-After 响应头支持，重试等待不再固定。
- 新增 GeoPackage（`.gpkg`）输出，几何统一为 MultiPolygon 并附 `.metadata.json`。
- 新增 `--building-values` 建筑类型过滤，并记录过滤与丢弃诊断统计。
- 新增 `--cache-dir` 分块缓存与 `--refresh-cache`，支持断点续跑。
- 新增 `--jobs` 并行分块查询，输出顺序与分块顺序一致。
- 新增 `pyproject.toml` 打包配置与 `osm-buildings`、`osm-buildings-gui` 两个命令行入口。
- 新增 PyInstaller 配置，可构建 `osm-buildings-cli.exe` 与 `osm-buildings-gui.exe`。
- CI 覆盖 Ubuntu 与 Windows，执行 ruff、mypy、unittest，并在 Windows 构建并上传 exe。
