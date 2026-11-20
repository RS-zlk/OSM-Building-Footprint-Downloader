# OSM Building Footprint Downloader

> OSM 建筑轮廓下载器

从 OpenStreetMap 的 Overpass API 下载指定范围内的建筑轮廓，并转换为标准 GeoJSON 或 Shapefile。支持当前或历史快照、SHP 研究区、自动坐标转换、分块下载和请求重试。

## Fork 与上游

- 本仓库：<https://github.com/eryieryiya/OSM-Building-Footprint-Downloader>，fork 自 <https://github.com/RS-zlk/OSM-Building-Footprint-Downloader>。
- 当前二次开发版本：2.1.0（开发中）。
- 开发分支为 `dev`；`main` 仅用于跟踪上游，不直接修改。
- 上游代码只读参考，不直接在上游仓库提交改动。

同步上游：

```bash
git fetch upstream
git rebase upstream/main
```

## 特性

- 下载当前或指定 UTC 时刻的 OSM 建筑轮廓。
- 按经纬度范围或 SHP Polygon/MultiPolygon 研究区查询。
- 自动识别 `.prj` 坐标系、转换至 WGS84，并严格裁剪输出。
- 处理普通建筑 way 和 multipolygon relation，保留孔洞。
- 为大范围自动分块，支持重试与 Overpass 端点故障切换。
- 提供命令行与桌面 GUI 两种工作方式。
- 可输出 GeoJSON 或 Shapefile（含 WGS84 `.prj`、UTF-8 `.cpg` 和元数据侧车文件）。

## 环境

推荐使用项目提供的 Conda 环境：

```bash
conda env create -f environment.yml
conda activate osm-buildings
```

## 用法

经纬度范围的顺序必须是：`南 西 北 东`（WGS84 / EPSG:4326）。程序会在范围较大时以 `0.05° × 0.05°` 网格自动分块，并对失败请求重试、在两个 Overpass 服务之间自动切换。

```bash
python3 download_buildings.py \
  --bbox 30.742 120.480 30.750 120.490 \
  --output data/wuzhen_buildings.geojson \
  --save-raw data/wuzhen_overpass.json
```

### 下载历史快照

通过 `--date` 指定历史时刻（ISO 8601，必须为 UTC）。例如下载 2024 年元旦时的建筑数据：

```bash
python download_buildings.py \
  --bbox 30.742 120.480 30.750 120.490 \
  --date 2024-01-01T00:00:00Z \
  --output data/wuzhen_buildings_2024.geojson
```

这依赖 Overpass 服务的 attic（历史）数据。默认服务可能对较早日期、较大范围或高负载查询拒绝或超时；此时请缩小范围、重试，或改用支持历史数据的 Overpass 实例（`--endpoint`）。输出 GeoJSON 的 `metadata.requested_osm_snapshot_utc` 会记录请求的快照时刻。

输出文件可直接用于 QGIS、GeoPandas、Leaflet 和 Folium。

### 输出 Shapefile

将输出文件扩展名设为 `.shp` 即可生成 Shapefile：

```bash
python download_buildings.py \
  --bbox 30.742 120.480 30.750 120.490 \
  --output data/wuzhen_buildings.shp
```

除 `.shp`、`.shx`、`.dbf` 外，程序还会生成 WGS84 坐标系文件 `.prj`、UTF-8 编码声明 `.cpg`、数据元信息 `.metadata.json` 和属性名映射 `.fields.json`。后两个文件用于保留 Shapefile 的字段名和元数据限制之外的信息；请将这些文件与 `.shp` 一并保存或分发。

## 图形界面

不熟悉命令行时，可在项目 Conda 环境中启动桌面界面：

```bash
conda activate osm-buildings
python gui.py
```

界面支持输入经纬度范围或选择 SHP 研究区、选择历史快照和输出位置，并会在后台执行下载，显示完成状态与错误日志。通过“保存文件类型”下拉框选择 GeoJSON 或 Shapefile，界面会自动匹配输出文件扩展名；SHP 模式沿用命令行的坐标系识别、范围裁剪、分块和重试逻辑。

- 经度和纬度使用四个独立输入框：南、西、北、东，不需要记忆空格或参数顺序。
- 快照可选择“最新数据”、常用相对日期，或输入任意 UTC 时刻。点击“选择日期”可从月历中选择日期；`«` 和 `»` 用于切换年份，`‹` 和 `›` 用于切换月份；时间按 `HH:MM:SS` 输入。
- 网格大小以度为单位；界面会依据范围中心纬度实时显示对应的南北、东西米制尺寸估算值。经度方向的实际距离会随纬度变化。
- 下载任务在后台执行，窗口保持可操作；完成或失败后可在运行日志中查看结果。

> [!NOTE]
> 历史快照的日期和时间均按 UTC 解释。数据是否可用取决于所选 Overpass 服务是否保留该时刻的历史数据。

### 使用 SHP 文件确定下载范围

使用 `--shp` 替代 `--bbox`。程序读取所有 Polygon/MultiPolygon 要素，依据同名 `.prj` 文件自动转换坐标系至 WGS84，并把结果严格裁剪到研究区边界。

```bash
python download_buildings.py \
  --shp data/study_area.shp \
  --output data/study_area_buildings.geojson
```

Overpass 查询仍使用研究区外接矩形以保证稳定性，但输出会严格裁剪到 SHP 边界，因此不会保留范围外建筑。若缺少 `.prj` 文件，可明确指定坐标系：`--shp-crs EPSG:3857`。

### 稳定性与大范围下载

```bash
python download_buildings.py \
  --bbox 30.742 120.480 30.850 120.600 \
  --tile-size 0.02 \
  --retries 3 \
  --endpoint https://overpass-api.de/api/interpreter \
  --endpoint https://overpass.kumi.systems/api/interpreter \
  --output data/buildings.geojson
```

- `--tile-size 0` 可关闭自动分块；`--max-tiles` 用于避免意外提交过大的任务。
- 多个 `--endpoint` 按顺序作为故障切换服务；不指定时使用内置的两个端点。
- 可使用 `--save-raw` 保存每个网格的原始 Overpass 响应，便于复现和排查。

## 数据范围与限制

- 查询对象：OSM 中标有 `building=*` 的闭合 way，以及标有 `building=*` 的 multipolygon relation。
- relation 仅在其成员几何由 Overpass 完整返回时导出；不完整 relation 会被跳过。
- 历史快照的可用年份和完整性取决于所用 Overpass 实例的历史数据保留情况。
- SHP 需要包含 Polygon/MultiPolygon；其 `.prj` 坐标参考文件应与数据一致。
- 覆盖率取决于当地 OSM 制图完整度；中国部分小区域的建筑标注可能较少。
- 请遵守 Overpass API 的合理使用原则：小范围查询、避免高频批量请求；大范围区域建议使用 Geofabrik PBF 数据。

## 数据署名与许可

数据来自 OpenStreetMap contributors，经 Overpass API 获取，遵循 [ODbL 1.0](https://opendatacommons.org/licenses/odbl/)。每个输出 GeoJSON 均写入数据来源、许可、查询范围与时间戳。

## 代码许可

本项目源代码采用 [MIT License](LICENSE) 发布。请注意，代码许可不改变所下载 OpenStreetMap 数据的署名和 ODbL 义务。

## 开源贡献

欢迎提交 issue 和 pull request。提交前请使用项目环境运行：

```bash
python -m unittest -v
```

请勿提交下载得到的大型数据文件；`data/` 已被 Git 忽略。使用 OSM 数据时，请保留相应的 OpenStreetMap 署名与 ODbL 许可信息。

详细流程请参阅 [贡献指南](CONTRIBUTING.md)，安全问题请参阅[安全策略](SECURITY.md)。
