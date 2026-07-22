# OSM 建筑矢量下载器

从 OpenStreetMap 的 Overpass API 下载指定范围内的建筑面，并转换为标准 GeoJSON。

## 环境

仅依赖 Python 3 标准库，无需安装第三方包。

## 用法

经纬度范围的顺序必须是：`南 西 北 东`（WGS84 / EPSG:4326）。

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

使用 `--shp` 替代 `--bbox`，程序会读取标准 ESRI Shapefile（`.shp`）文件头中的外接范围。SHP 坐标必须为 WGS84 经纬度（EPSG:4326）。

```bash
python download_buildings.py \
  --shp data/study_area.shp \
  --output data/study_area_buildings.geojson
```

当前按 SHP 的最小外接矩形向 Overpass 查询，因此不规则研究区外、但仍位于该矩形内的建筑也会包含在结果中；如需严格按边界裁剪，可在 QGIS 等 GIS 软件中对输出进行裁剪。

## 数据范围与限制

- 查询对象：OSM 中标有 `building=*` 的闭合 way（建筑面）。
- 复杂建筑 relation / multipolygon 当前不导出；通常占比较低，但需要完整覆盖时可另行扩展。
- 历史快照的可用年份和完整性取决于所用 Overpass 实例的历史数据保留情况。
- 使用 SHP 文件时，文件坐标系必须是 WGS84（EPSG:4326）；本工具不自动执行坐标转换。
- 覆盖率取决于当地 OSM 制图完整度；中国部分小区域的建筑标注可能较少。
- 请遵守 Overpass API 的合理使用原则：小范围查询、避免高频批量请求；大范围区域建议使用 Geofabrik PBF 数据。

## 数据署名与许可

数据来自 OpenStreetMap contributors，经 Overpass API 获取，遵循 [ODbL 1.0](https://opendatacommons.org/licenses/odbl/)。每个输出 GeoJSON 均写入数据来源、许可、查询范围与时间戳。
