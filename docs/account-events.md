# 账户事件台账

R03 的前置记录能力：调用者明确登记入出金、现金分红和拆并股。事件为独立追加记录，不改写实际成交、手工持仓或成本，也不计算账户收益。当前没有账户事件页面。

## 离线命令

关闭该工作空间的 StockSeek 后，在仓库根目录运行；将路径和标识替换为本地值。工作空间必须已存在，正在运行或恢复不完整时拒绝访问。JSON 使用 UTF-8（支持 BOM），最大 64 KiB。

```powershell
.\.venv\Scripts\python.exe -m tradingagents.desktop.account_events --workspace <工作空间> record cash_transfer <事件.json>
.\.venv\Scripts\python.exe -m tradingagents.desktop.account_events --workspace <工作空间> get <record_id>
.\.venv\Scripts\python.exe -m tradingagents.desktop.account_events --workspace <工作空间> void <作废.json>
.\.venv\Scripts\python.exe -m tradingagents.desktop.account_events --workspace <工作空间> list --broker <券商标识> --account-ref <账户别名> --kind cash_dividend --limit 100 --offset 0
```

桌面 API 对应 `record_account_event(kind, payload)`、`get_account_event(record_id)`、`list_account_events(broker, account_ref, kind, limit, offset)`。查询按登记顺序倒序，分页总数与记录取自同一数据库快照；空结果不表示账户没有事件或没有风险。

## 字段与含义

三类事件均必须包含以下字段，不能增加模板外字段：

| 字段 | 含义 |
| --- | --- |
| `broker` | 券商或账户记录来源标识 |
| `account_ref` | 本地账户别名，不填认证凭据 |
| `event_id` | 该券商账户下跨日期、跨类型唯一的事件标识 |
| `occurred_at` | 调用者声明的事件生效时间，必须带时区，另存 UTC；不是首次公开时间或系统获知时间 |
| `source_label` | 对账单、凭证或手工记录的来源说明，不代表已核验 |

各类型还必须包含：

| 类型 | 额外字段 |
| --- | --- |
| `cash_transfer` | `currency`、`direction`（`deposit` / `withdrawal`）、正数 `amount` |
| `cash_dividend` | `ticker`、`currency`、正数 `gross_amount`、`tax`（非负数或 `null`） |
| `split` | `ticker`、正数 `ratio_before`、正数 `ratio_after` |

数值必须为十进制字符串，不能用浮点数、科学计数法或负号；币种为三位大写字母。分红金额和税额均为该次账户事件总额，使用相同币种，不能填每股股息。`tax: null` 表示未知，`"0"` 才表示明确为零；尚不计算税后净额。拆并股比例表示“原有多少份 → 变为多少份”，例如 1 → 2 或 10 → 1；不代表已验证证券身份、实际数量、碎股现金或成本调整。

合成入金示例（请勿将真实账户事件提交源码仓库）：

```json
{
  "broker": "synthetic-broker",
  "account_ref": "synthetic-account",
  "event_id": "synthetic-deposit-1",
  "occurred_at": "2024-01-02T10:15:00+08:00",
  "source_label": "合成账户记录",
  "currency": "CNY",
  "direction": "deposit",
  "amount": "100.00"
}
```

同一券商、账户及事件标识重试，规范化内容相同则返回原记录；改变类型、日期、金额或来源会报冲突，不覆盖历史。来源若只提供每天重复的流水号，调用者须先为其构造稳定且跨日期唯一的标识。存储保留原始输入、规范化数据、登记时间与内容指纹，重试不替换原始输入。没有自动抓取或券商对账流程；不要换标识重复登记同一经济事件来绕过冲突。

## 错误记录作废

`void` 仅作废本地误录，不向券商发送撤单、退款或其他指令。它追加原因、操作人、系统登记时间及内容指纹，保留原始事件原文；不会撤销实际经济行为或调整持仓。若真实发生了反向资金流，应按实际凭证另记事件，不能将原事件作废代替。

作废 JSON 只能包含以下四个字符串字段，均必填，每项最多 200 字符：

```json
{
  "record_id": "待作废记录的实际 record_id",
  "request_id": "synthetic-void-request-1",
  "reason": "合成记录误录，原始凭证没有此笔事件",
  "recorded_by": "合成操作人"
}
```

桌面 API 为 `void_account_event(record_id, request_id, reason, recorded_by)`。请求标识在整个工作空间唯一；相同内容重试返回原作废记录，改变目标、原因或操作人均报冲突。同一事件只能作废一次，不能恢复或覆盖作废说明。并发写入采用事务，失败不占用请求标识。

登记、查询及列表返回当前 `ledger_status: recorded` 或 `voided`；`void` 字段分别为 `null` 或完整作废记录。`recorded` 仅表示尚未作废，不代表券商确认或经济有效。列表包含作废历史，总数包括作废事件，不能直接将所有记录汇总。原登记请求重试仍显示已作废，不会重新激活事件；原事件指纹保持不变，作废记录有独立指纹。系统作废时间不改变原事件发生时间，不构成历史时点已知信息。

## 原子更正

误录需要替代记录时，使用 `correct`，一次事务完成新事件登记、原事件作废与更正关联。任一步失败均回滚，原记录保持原状。更正 JSON 必须包含上述四个作废字段，另加替代事件的 `kind` 和完整 `payload`：

```json
{
  "record_id": "待更正记录的实际 record_id",
  "request_id": "synthetic-correction-1",
  "reason": "合成记录金额误录",
  "recorded_by": "合成操作人",
  "kind": "cash_transfer",
  "payload": {
    "broker": "synthetic-broker",
    "account_ref": "synthetic-account",
    "event_id": "synthetic-deposit-1-corrected",
    "occurred_at": "2024-01-02T10:15:00+08:00",
    "source_label": "合成更正凭证",
    "currency": "CNY",
    "direction": "deposit",
    "amount": "101.00"
  }
}
```

关闭应用后执行（JSON 文件限制与登记相同）：

```powershell
.\.venv\Scripts\python.exe -m tradingagents.desktop.account_events --workspace ./output/dev-workspace correct ./output/correction.json
```

桌面 API 为 `correct_account_event(record_id, request_id, reason, recorded_by, kind, payload)`。替代事件必须使用原券商及账户下尚未登记的新事件标识；不能复用已有事件，即使内容相同。账户身份误录应另行核实并作废、登记，当前不支持跨账户更正。已作废的事件不能再次更正，不能恢复事件。更正与独立作废共用工作空间请求标识，不能借更正覆盖已有请求；同一更正的规范化内容重试返回原更正凭证，改变目标、替代内容、原因或操作人均拒绝。

事件查询与分页新增 `replaces`（它替代的原记录 ID）及 `replaced_by`（替代它的记录 ID），无关联时为 `null`。替代事件以后仍可更正，形成可逐条查询的追加链；原始事件内容与指纹始终保留。更正凭证是固定的历史记录，重试返回它不表示替代事件目前仍有效，应再查询事件状态。原事件的 `void` 同时包含替代记录 ID。列表保留整条历史，不能把链中各记录当成独立经济事件相加。

备份同时保留原事件、作废及更正记录，旧工作空间首次使用时追加对应表。更正仅处理本地误录，不发送券商指令、不调整持仓或计算收益，仍须用实际凭证核实。

## 绩效与完整性限制

每条记录都明确返回 `verification: caller_declared_unverified`、`effects: not_applied`、`account_completeness: not_established` 和 `performance: not_evaluated`。事件生效时间不能证明事前已知，不用于历史策略或回测补值。

账户可能缺少期初资产、现金余额、完整成交、费用、汇率、税款、其他公司行动及估值行情。禁止把未登记视为零，不汇总不同账户或币种，不自动复权，不宣称价格收益、总回报、资金加权或时间加权收益。R03 的完整账户核验和 R06 的收益计算仍为待办。

台账保存在工作空间 SQLite 中，纳入[工作空间备份与恢复](workspace-backup.md)。备份含个人账户信息，请按个人数据保管，不提交仓库或公开分发。
