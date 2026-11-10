# 策略独立对照与人工分歧

在策略版本详情中先选择一个条件检查快照，再在“策略独立对照”填写另一检查的完整标识。可从另一个策略或版本的条件快照复制标识。读取后并列核对两边策略名称、版本、来源、环境、持有期，以及逐条规则、状态、计算值、单位、出处和观测证据。不会请求行情或模型。

预览与保存均检查证券、市场、周期、价格口径、数据来源、修订、截止时刻及完整输入序列的差异。截止时刻按实际时间比较；序列按规范 JSON 内容指纹比较，包括单位、观测、可得时间和输入顺序。差异提示只描述输入上下文，不推断两套规则冲突；字段相同也不证明规则含义、适用环境或数据质量等价。

人工判断为“未评估”“人工识别到分歧”或“人工未识别到分歧”。具名保存；后两者必须填写依据、分歧条件与适用环境。个人判断与两个独立检查结果分别保留，不平均评分、不合成信号、不选择执行优先级。单条条件相反、缺失、过期或未实现都不会自动变成策略冲突。判断不会修改策略、研究评级、持仓、成交或回测状态。

每次保存追加不可变对照快照，包含原策略确切版本、完整条件检查、上下文差异、具名判断和限制。同样两项有序检查及相同判断内容重试只返回原快照；修改意见会产生新快照，不覆盖旧意见。双方检查都能读取这条历史。切换检查或版本时隔离输入与结果；当前窗口保留失败输入并可重试。刷新窗口后未保存输入丢失，已保存记录可通过“读取 / 刷新此检查的对照历史”恢复。

桌面 API：

- `preview_strategy_comparison(left_id, right_id)`：读取两项独立证据和上下文差异，不保存对照。
- `save_strategy_comparison(left_id, right_id, verdict, note, assessed_by)`：`verdict` 分别为 `not_assessed`、`conflict_identified`、`no_conflict_identified`；两个检查必须存在且不同，可来自不同策略或同策略不同版本。
- `get_strategy_comparison(comparison_id)`：读取完整快照。
- `strategy_comparison_history(check_id)`：读取涉及指定检查的历史摘要，按本地追加顺序倒序排列。

记录随工作空间 SQLite 备份。当前仅支持两项对照和具名个人判断，没有自动语义冲突识别、冲突化解、批量筛选或真实策略试点验收，S09 整项保持未完成。

离线验证（PowerShell，仓库根目录）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_desktop_strategy_comparisons.py -q
$env:PYTHONPATH='.'
.\.venv\Scripts\python.exe desktop/strategy_comparisons_smoke.py --chromium-path 'C:\Program Files\Google\Chrome\Application\chrome.exe'
```
