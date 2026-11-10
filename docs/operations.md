# 计划与模拟记录

`OperationLedger` 仅登记意图或调用者声明的模拟结果，不执行交易、不生成真实成交、不改写手工持仓或研究结论。真实成交必须使用独立的 [实际成交导入](trade-import.md)。三类记录保留固定类型，无升级、覆盖或删除接口；模型输出也不能改写已登记的操作。当前没有管理页面、自动撮合、计划状态流转或计划遵守评分。

记录格式使用严格字段集合，不能夹带 `kind`、券商成交号或其他额外字段。文本为单行且不含控制字符。数量、价格、费用使用十进制字符串；数量、价格为正，费用非负；不接受浮点数、指数或 NaN。币种为三个大写字母，时间必须有时区。`source_label` 为本地来源说明，`book_ref` 为虚拟账本别名，不代表已核验券商账户。以下全部为合成示例。

计划 JSON（`plan.json`）：

```json
{
  "book_ref":"synthetic-virtual-book",
  "ticker":"600941", "market":"CN-A", "side":"buy",
  "quantity":"100", "currency":"CNY", "source_label":"synthetic-personal-input",
  "planned_for":"2024-01-02T10:15:00+08:00", "limit_price":null,
  "reason":"合成计划理由"
}
```

`limit_price=null` 表示尚未指定计划限价，不是市价订单授权。`planned_for` 是调用者填写的意图时间；当前登记时间独立保存，补录不会证明历史上已有此计划。`reason` 必填，最多 10000 字符。

模拟 JSON（`simulation.json`）：

```json
{
  "book_ref":"synthetic-virtual-book",
  "ticker":"600941", "market":"CN-A", "side":"buy",
  "quantity":"100", "currency":"CNY", "source_label":"synthetic-offline-input",
  "simulated_at":"2024-01-02T10:15:00+08:00", "price":"65.125", "fees":null,
  "assumptions":"合成假设：忽略滑点，未核验涨跌停或成交量"
}
```

`fees=null` 明确未知，`"0"` 明确为零；`assumptions` 必填，最多 10000 字符。这是模拟结果登记入口，不计算成交价格、不验证市场可成交性、费用口径或汇率，不可据此宣称回测有效或完整账户收益。固定 `execution=simulated_only`；计划固定 `execution=not_executed`。两者均标记调用者声明、历史知识状态未确立。

## 离线命令

先关闭目标工作空间的桌面应用。命令只使用已存在且恢复完整的 `workspace.sqlite3`，拒绝运行中工作空间；JSON 为 UTF-8（可含 BOM），最大 64 KiB。

```powershell
.\.venv\Scripts\python.exe -m tradingagents.desktop.operations --workspace D:\StockSeek\workspace record plan D:\StockSeek\plan.json --request-id synthetic-plan-001
.\.venv\Scripts\python.exe -m tradingagents.desktop.operations --workspace D:\StockSeek\workspace record simulation D:\StockSeek\simulation.json --request-id synthetic-sim-001
.\.venv\Scripts\python.exe -m tradingagents.desktop.operations --workspace D:\StockSeek\workspace list --kind plan --ticker 600941 --limit 100 --offset 0
.\.venv\Scripts\python.exe -m tradingagents.desktop.operations --workspace D:\StockSeek\workspace get OPERATION_ID_FROM_RECORD
```

记录返回 `operation_id`，查询不含临时重试状态。请求标识在同一类型中唯一；同一标识、同一规范化内容重试返回原记录，`already_recorded=true`；不同内容会报错，不覆盖旧记录。不同请求标识视作新的独立记录，不自动判断经济内容重复。更正意图应登记新计划并自行保留对应说明，当前无撤销生命周期功能。分页上限 1000，可省略类型查询全部非真实操作，但不会混入实际成交。

输出含私人计划或模拟假设，请勿进入公共日志。记录位于独立 `non_actual_operations` 表，随 [工作空间备份](workspace-backup.md) 一并恢复。原始输入的数字格式会规范化，内容指纹针对规范化字段，不声称保存完整文件字节或防篡改。

桌面桥接使用标准 `{ok, data/error}` 响应：`record_operation(kind, payload, request_id)`、`get_operation(operation_id)`、`list_operations(kind=None, ticker=None, limit=100, offset=0)`。Python 服务对应 `OperationLedger(store).record/get/list`；仅接受 `plan/simulation`，拒绝 `actual`，不调用模型或券商。
