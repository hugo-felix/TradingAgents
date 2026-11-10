# 实际成交 CSV 导入

当前提供离线命令、Python 服务及桌面桥接，尚无导入页面或券商格式自动转换。只接收调用者声明的已发生逐笔成交，固定标记 `kind=actual`、`verification=caller_declared_unverified`，不代表已经与券商核验。不要向该入口提交计划或模拟成交。现有手工持仓、成本、个人判断、研究报告和策略定义不会被导入改写，也不会据此计算完整账户收益。

CSV 使用 UTF-8 文本，可有 BOM；最大 8 MiB、10000 条记录。列名必须恰好包含下列字段，顺序可变，不能重复或增加列：

```csv
broker,account_ref,execution_id,trade_date,executed_at,ticker,market,side,quantity,price,currency,fees
synthetic-broker,demo-account,fill-001,2024-01-02,2024-01-02T10:15:00+08:00,600941,CN-A,buy,100,65.125,CNY,
```

上例仅为合成数据。`account_ref` 建议使用稳定的本地账户别名，不填密码或令牌。`execution_id` 必须是券商逐笔成交号，不是可能对应多笔成交的委托号。成交身份由券商、账户别名、交易日及逐笔成交号共同确定；换别名可能造成无法识别的重复，应先核对映射。

`trade_date` 为 `YYYY-MM-DD`，必须与 `executed_at` 所带时区的本地日期相同；保存时同时保留原字段与转换后的 UTC 时间。`ticker` 使用现有股票代码规范化；`market` 为调用者声明的市场。方向只接受 `buy/sell`。数量和价格必须为正的十进制字符串，费用非负；不接受指数、千位逗号、负数、NaN 或无穷值，保存精确十进制字符串。币种为三个大写字母；费用必须已换成相同币种，当前不核验市场、币种或汇率，也不生成换汇记录。

费用为空表示未知，`0` 表示明确为零，两者不同。相同成交号后来补费用属于内容冲突，当前拒绝覆盖；更正、撤销、拆分费用及券商专用适配尚未实现，不应通过更改成交号绕过去重。

## 离线命令

先关闭目标工作空间的桌面应用，确认目录中已有 `workspace.sqlite3`。命令拒绝运行中、不存在或恢复未完成的工作空间，不会因目录输错而创建新库。以下路径均为示例；在仓库根目录执行：

```powershell
# 预览完整规范化行、新增、重复、冲突和缺费用信息，记录输出的 sha256
.\.venv\Scripts\python.exe -m tradingagents.desktop.trades --workspace D:\StockSeek\workspace preview D:\StockSeek\fills.csv --source broker-export-2024-01

# 核对后，将示例占位指纹替换为预览的实际 sha256
.\.venv\Scripts\python.exe -m tradingagents.desktop.trades --workspace D:\StockSeek\workspace import D:\StockSeek\fills.csv --source broker-export-2024-01 --sha256 SHA256_FROM_PREVIEW

.\.venv\Scripts\python.exe -m tradingagents.desktop.trades --workspace D:\StockSeek\workspace list --ticker 600941 --limit 100 --offset 0
.\.venv\Scripts\python.exe -m tradingagents.desktop.trades --workspace D:\StockSeek\workspace batch BATCH_ID_FROM_IMPORT
```

导入输出只返回批次摘要；`batch` 返回完整原文和行级审计。预览、查询及审计输出会包含输入的账户别名和成交信息，勿提交到公共日志或源码仓库。失败以非零退出码报告；修正输入后重新预览，不覆盖旧成交。

## 服务调用流程

服务为 `TradeLedger(store)`，其中 `store` 为桌面工作空间的 `DesktopStore`。桌面桥接沿用 `{ok, data/error}` 响应：

1. `preview_actual_trades(csv_text, source_label)` 校验整批，返回文本 SHA-256、新增、重复、冲突及缺费用数量，`items` 含原始和规范化字段。预览不保存成交或导入批次；首次调用会建立独立台账表。
2. 人工核对后，`import_actual_trades(csv_text, source_label, expected_sha256)` 传入相同文本、来源标签及预览指纹。指纹不一致拒绝导入；导入时重新校验并在事务内检查现存成交。指纹只检测文本变化，不是权限或审批令牌。
3. `list_actual_trades(ticker=None, limit=100, offset=0)` 查询成交，总数与条目一并返回，按成交时间从新到旧分页；`limit` 为 1–1000。
4. `get_trade_import(batch_id)` 读取原始批次审计信息，包括完整 CSV 文本、逐行原值、规范化字段、文本指纹、来源标签及本地登记时间。

Python 服务对应 `preview`、`import_csv`、`list_fills`、`get_import`。同一来源标签与同一文本重复导入返回原批次，并标记 `already_imported=true`。跨文件或同批的同身份、同规范化内容只保存一笔成交；原批次仍保留所有输入行。相同身份但不同内容拒绝整批，事务回滚，不保留前面已插入的行。并发导入也使用同一事务约束。

数据只追加到 `actual_fills` 与 `actual_fill_imports`，不提供覆盖或删除接口。来源标签和记录时间属于本地登记证据，不证明实际成交或历史上已经持有某项判断。原文按传入文本保存，指纹是该文本的 UTF-8 字节指纹，不声称保留券商文件的原始编码字节。包含敏感账户信息的工作空间及其备份应自行妥善保管，不提交源码仓库。

工作空间备份包含台账和审计记录，使用说明见 [备份与恢复](workspace-backup.md)。计划与模拟使用 [独立非真实操作台账](operations.md)，不混入成交。现金、分红、公司行动、完整收益及对账界面仍未实现。

## 成交依据与个人理由

每笔实际成交可追加独立的关联版本，支持只填个人理由或完全没有关联的历史成交。报告和策略均保存原始数据库 JSON 文本、解析内容及该文本的 UTF-8 SHA-256；策略必须指定已确认的明确版本。随后修改策略或查询报告，不会重写旧关联。指纹用于核对快照内容，不代表整个工作空间受到防篡改保护。

报告必须属于同一股票，分析日期不得晚于成交日期。报告在成交前已登记时标记 `availability=recorded_before_trade`；晚导入的历史报告标记 `not_established`。策略版本的创建、确认时间均不得晚于成交时间。所有关联仍固定为 `knowledge_at_trade=not_established`、`usage=personal_review_only`，登记时间是当前补录时间，不证明当时已知、实际交易原因或历史回测可用性，也不会调用模型或更改成交、持仓、报告和策略。

在 UTF-8 JSON 文件中填写完整的新关联，例如以下合成补录内容。字段可省略或为 `null`；个人理由为不含控制字符的单行文本，最多 10000 字符。有策略标识时必须同时填写正整数版本：

```json
{"personal_reason":"合成补录理由", "report_id":null, "strategy_id":null, "strategy_version":null}
```

关闭工作空间后执行，替换实际成交标识与文件路径：

```powershell
.\.venv\Scripts\python.exe -m tradingagents.desktop.trades --workspace D:\StockSeek\workspace get TRADE_ID
.\.venv\Scripts\python.exe -m tradingagents.desktop.trades --workspace D:\StockSeek\workspace context TRADE_ID
.\.venv\Scripts\python.exe -m tradingagents.desktop.trades --workspace D:\StockSeek\workspace context-save TRADE_ID D:\StockSeek\context.json --expected-version 0
.\.venv\Scripts\python.exe -m tradingagents.desktop.trades --workspace D:\StockSeek\workspace context TRADE_ID --version 1
.\.venv\Scripts\python.exe -m tradingagents.desktop.trades --workspace D:\StockSeek\workspace context-history TRADE_ID --limit 100 --offset 0
```

JSON 文件最大 64 KiB。第一次保存预期版本为 0，以后使用查询得到的最新版本；过期写入会失败，应先重新读取再决定是否重试。每次保存都是完整的新版本，省略字段或填写 `null` 只解除新版本的关联，旧版本仍可读取；不提供覆盖和删除。已存在但未补录的成交查询关联返回 `null`，历史列表为空；未知成交会报错。输出可能包含私人理由和报告全文，请妥善保管。

实际成交还可登记[个人复盘与规则草案](trade-reviews.md)，分别记录研究、规则、执行意见及不确定性，保留登记时关联快照；不会改写历史关联或自动判定交易对错。

入出金、现金分红和拆并股请使用独立的[账户事件台账](account-events.md)。这些调用者声明记录不自动调整持仓或成本，尚不构成完整账户收益数据。

桌面桥接提供 `get_actual_trade(trade_id)`、`record_trade_context(trade_id, context, expected_version=0)`、`get_trade_context(trade_id, version=None)` 和 `trade_context_history(trade_id, limit=100, offset=0)`。Python 服务为 `TradeContextRegistry(store)` 的 `record/get/history`，保存到独立的 `actual_fill_contexts` 表，随工作空间备份恢复。当前没有关联管理页面。
