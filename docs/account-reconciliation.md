# 离线账户对账

对账比较调用者提供的外部凭证条目与本地实际成交、现金入出、现金分红及拆并股台账。只保存比较结果，不自动导入、作废、更正记录或调整持仓，不连接券商，不计算收益。

JSON 模板（以下均为合成数据）：

```json
{
  "broker": "synthetic",
  "account_ref": "account",
  "period_start": "2024-01-02T00:00:00+08:00",
  "period_end": "2024-01-03T00:00:00+08:00",
  "source_label": "合成对账单",
  "categories": ["cash_transfer"],
  "entries": [{
    "kind": "cash_transfer",
    "payload": {
      "broker": "synthetic", "account_ref": "account", "event_id": "e1",
      "occurred_at": "2024-01-02T10:00:00+08:00", "source_label": "合成凭证",
      "currency": "CNY", "direction": "deposit", "amount": "100.00"
    }
  }]
}
```

`categories` 必须明确选择 `actual_fill`、`cash_transfer`、`cash_dividend`、`split` 中一种或多种，不重复。仅核对声明范围：同一券商和账户，起点包含、终点不包含，所有时间必须含时区。超出范围的外部条目会被拒绝。空条目列表允许用于核对本地是否多录，不能据此认定账户没有活动。上限 8 MiB、10000 条。

成交 payload 使用 [成交 CSV](trade-import.md) 的全部列名，所有值为字符串，未知费用为空字符串。事件 payload 使用 [账户事件](account-events.md) 的完整字段，未知税额为 `null`。来源标签也是事件内容的一部分，需保留同一凭证的原始标签；更换标签会形成内容差异。数值和时间先按既有台账规则规范化，未知费用或税额与明确零值不同。

关闭目标工作空间后运行（不要将实际凭证提交到源码仓库）：

```powershell
.\.venv\Scripts\python.exe -m tradingagents.desktop.account_reconciliation --workspace ./output/dev-workspace record ./output/statement.json --request-id reconciliation-1 --recorded-by reviewer
.\.venv\Scripts\python.exe -m tradingagents.desktop.account_reconciliation --workspace ./output/dev-workspace get reconciliation-1
.\.venv\Scripts\python.exe -m tradingagents.desktop.account_reconciliation --workspace ./output/dev-workspace list --limit 20 --offset 0
```

CLI 拒绝不存在、运行中或未完整恢复的工作空间。桌面 API 提供 `record_account_reconciliation(statement, request_id, recorded_by)`、`get_account_reconciliation(request_id)`、`list_account_reconciliations(limit, offset)`，目前没有专用管理页面。

每次核对在同一数据库事务中保存原始对账单、规范化条目、本地条目及其指纹、被排除的作废记录与更正关联、操作人和时间。结果分别列出：一致、本地遗漏、外部凭证遗漏、同标识内容不一致、凭证内重复、凭证内冲突。成交身份沿用券商/账户/交易日期/成交标识；事件身份沿用券商/账户/事件标识，跨事件类型仍可识别内容冲突。冲突身份单独展示外部所有版本和本地记录，不任意选择一个版本。

同一请求标识、完全相同的 JSON 内容和操作人重试返回原快照，即使本地台账后来有变；不同内容或操作人拒绝覆盖。需要重新核对时使用新的请求标识。快照与台账一起进入既有备份恢复流程。

`no_differences_in_declared_scope` 只表示这次声明范围内未发现差异。外部凭证仍为调用者声明，未与券商核验；遗漏的记录类型、期初余额、估值、汇率及其他公司行动不在此比较中。`uncertainties` 保留未知费用和税额；账户完整性保持 `not_established`，绩效保持 `not_evaluated`。存在差异时应先人工检查凭证，再通过现有导入或事件更正流程处理，另建请求复核。
