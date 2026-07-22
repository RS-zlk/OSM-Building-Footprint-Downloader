# 贡献指南

感谢你愿意改进 OSM 建筑矢量下载器。

## 开始之前

1. 先搜索现有 issue，避免重复工作。
2. 较大的功能改动请先创建 issue 说明目标和方案。
3. 不要提交下载得到的建筑数据、访问令牌或个人路径。

## 本地开发

```bash
conda env create -f environment.yml
conda activate osm-buildings
python -m unittest -v
```

## 提交拉取请求

- 每个 PR 聚焦一个明确的改动。
- 为行为变化补充或更新测试。
- 保持 README 与界面、命令行行为一致。
- 在提交前运行 `python -m unittest -v`。
- 说明测试结果，以及涉及 Overpass 请求时使用的范围和端点。

## 数据与服务使用

请遵守 OpenStreetMap 的署名与 ODbL 要求，并避免对 Overpass API 发起大范围、高频请求。测试应优先使用本地模拟数据，避免依赖公共服务的实时响应。
