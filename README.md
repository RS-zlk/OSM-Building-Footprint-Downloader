# OSM 建筑矢量下载器

从 OpenStreetMap 的 Overpass API 下载指定范围内的建筑轮廓，并转换为标准 GeoJSON。支持当前或历史快照、SHP 研究区、自动坐标转换、分块下载和请求重试。

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
