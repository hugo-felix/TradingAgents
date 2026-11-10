# 来源日期与修订证据

`dataflows/facts.py` 的 `Fact` 可携带可选的 `provenance`。既有无此字段的事实继续有效，工具 artifact 的 `fact_schema_version` 保持 1。元数据随工具证据保存、进入 grounded report 的事实目录，并由 `render_facts` 按来源值展示；已有报告快照不会重抓或重写。

| 字段 | 含义 |
| --- | --- |
| `period_start` / `period_end` | 数值覆盖期间的起止日期；时点余额只有结束日，不推算开始日 |
| `published_date` | 当前选中修订的公开日期；不等同于该指标首次公开日，也不提供日内时刻 |
| `first_published_date` | 已证实的首次公开日期；无法证实时为空 |
| `fetched_at` | 本系统获取这份供应商快照的带时区时间；不代替公开日期 |
| `revision` | 供应商提供的版本身份；缺失时为空 |
| `snapshot_sha256` | 供应商快照的 SHA-256；具体字节规范由适配器说明 |

期间开始不得晚于结束；已知首次公开日不得晚于修订公开日；获取时间必须带时区。未知值保留为空，不能用期间末日、缓存修改时间或当前时间补成已知公开/获取时间。

## SEC EDGAR 接入范围

三张财务报表的结构化结果与文本共用同一份申报选择结果：按请求日期选择当时已申报的版本，保留季度与累计期间的既有筛选规则。财务数值按原始单位存储（例如 USD、USD/shares），文本 CSV 沿用百万美元展示。不存在用百万美元缩放后的值充当原始金额的第二套事实。

- `published_date` 来自该条目的 `filed`，`revision` 来自 `accn`。未提供 accession 时不制造版本号。该次申报可能重述早期期间，不能把 `filed` 当作首次公开时间；当前实现不推断 `first_published_date`。
- `snapshot_sha256` 对整个解码后的 companyfacts 对象使用 UTF-8、`ensure_ascii=False`、键排序、紧凑分隔符、不允许非有限 JSON 数字后计算。它不是 HTTP 原始字节指纹，也不是单个条目的指纹。
- 缓存使用带版本的 JSON envelope，将 payload 与实际获取完成时间原子保存；命中不会刷新获取时间。24 小时 TTL 仍按缓存文件时间判断。旧的裸 JSON 缓存可继续读取，获取时间显示未知；到期后按新格式刷新。不迁移或扫描用户缓存。
- 同一财务行不同期间出现互不兼容的单位时，适配器报供应商不可用，交由既有路由处理；不把不同币种历史合并后统一贴上最后一个单位。

此实现提供按**日期**筛选的来源证据，不能据此批准申报当天开盘或其他日内成交。`filed` 的精度不足以证明该时刻已经公开。当前 ticker 映射仍是当前名单。其他供应商的修订历史与快照归档、首次公开时间核验、历史证券池与统一历史时点查询尚未完成，因此 B04 整项仍未完成。

## SEC 财报证据归档

财报适配器使用的 companyfacts 对象另存到配置的 `data_cache_dir/sec_edgar/snapshots/<snapshot_sha256>.json`。缓存命中也会归档；后续缓存刷新不会替换旧指纹对应的对象。相同内容只保存一份，不把重复获取时间当作新的数据版本。这里只保存实际参与财报查询的解码 JSON，不是所有 HTTP 响应或逐次获取事件的完整档案。

`sec_edgar.load_companyfacts_snapshot(snapshot_sha256)` 可按事实里的指纹读取原对象，读取前校验字节指纹及规范化格式，不发起供应商请求。缺失或损坏抛出 `SnapshotIntegrityError`；正常财报路径遇到归档写入或核验失败也报供应商不可用，沿用路由错误处理，不声称已保留证据。不静默联网修补损坏的旧文件，不覆盖原文件。

归档通过文件锁和临时文件原子发布；有效对象不会由本应用覆盖。它不是只写存储，也不防止手工删除、修改或磁盘丢失。**备份报告时应同时备份上述 snapshots 目录**；清理数据缓存时应保留它。指纹仍须与报告中的来源日期、版本和获取时间一同使用；仅有归档不能重放完整研究或证明首次公开时刻。

离线回归：`tests/test_data_snapshots.py`、`tests/test_sec_edgar.py`、`tests/test_facts.py`、`tests/test_grounding.py`、`tests/test_fact_transport.py`、`tests/test_cache_writes.py`。不通过这些测试声称线上数据覆盖或服务许可已验证。
