# 个人复盘与规则草案

`ReviewRegistry` 和桌面 Python API 支持对已导入的实际成交登记个人复盘。当前没有复盘页面、自动总结、模型调用、收益计算或自动评价。所有内容均由调用者明确填写，不将卖后上涨、单笔盈亏等同于研究或执行错误。

复盘分别保留 `research`（研究判断）、`strategy`（规则）、`execution`（个人执行）和 `uncertainty`（证据不足及其他解释）。前三项允许 `null` 表示尚未评估；不表示通过、无风险或零分。`uncertainty` 必须为非空文本。每项至多 10000 字符且不得换行。

桌面桥接接口遵循现有 `{ok, data/error}` 返回格式：

- `record_trade_review(trade_id, notes, request_id)`：仅支持实际成交。请求标识全局唯一；相同标识和规范化内容重试返回原复盘，不重新采集快照。冲突内容拒绝覆盖。修正意见需使用新的标识登记新复盘。
- `get_trade_review(review_id)`：读取不可变复盘及登记时实际成交、最新关联版本快照。后续关联补录不会改写此快照。
- `list_trade_reviews(trade_id=None, limit=100, offset=0)`：按登记顺序从新到旧分页查询，可按实际成交标识过滤。`limit` 为 1–1000，`offset` 为非负整数；同一秒登记的复盘也保持追加顺序。
- `create_review_strategy(review_id, definition)`：由用户明确提供现有策略格式的规则定义，每次复盘仅生成一个初始策略草案。重复或并发生成会拒绝，之后通过已有策略编辑流程追加版本。

本地 Python 使用示例（工作空间须关闭，勿对运行中的数据库操作）：

```python
from tradingagents.desktop.instance import WorkspaceLock
from tradingagents.desktop.reviews import ReviewRegistry
from tradingagents.desktop.store import DesktopStore

workspace = 'output/review-workspace'  # 使用已有的隔离测试工作空间
with WorkspaceLock(workspace):
    registry = ReviewRegistry(DesktopStore(workspace))
    review = registry.record('已导入的实际成交标识', {
        'research': None,
        'strategy': '尚无足够样本判断规则有效性',
        'execution': '尚未取得原始计划证据',
        'uncertainty': '单笔结果与市场共同波动不能证明因果',
    }, '由调用者生成的唯一请求标识')
```

复盘固定标记为个人意见，历史知识与因果关系未确立，不作为回测输入。系统时间记录当前登记时间，不接受回填日期。复盘保留在本地数据库，备份包含其内容及规则中的复盘快照；不要提交真实复盘和备份到源码仓库。

生成草案时，所有提出的规则均强制标为 `personal`、`pending`，出处增加复盘标识；保留原来的假设说明，并附完整复盘快照及 SHA-256。即便调用者提交 `author` 或 `defined`，也不能通过此入口取得原作者身份或确认状态。草案仅面向未来，不能改变过去的成交、报告、关联、知识状态或策略版本。

用户在策略管理页面核对、补齐范围和规则，并将待定义规则明确改为人工判断或已定义状态，保存新版本后，才能具名确认。确认会再追加版本；不代表实现、回测、样本外验证或交易授权。当前不自动检查用户判断是否正确，也不提供完整账户归因。

## 离线命令

先关闭使用该工作空间的应用；命令取得工作空间锁，拒绝不存在或未完整恢复的工作空间，不新建空库。输入文件为 UTF-8 JSON（允许 BOM），最多 64 KiB。以下路径及标识均为占位值，使用已导入的实际成交标识：

```powershell
.\.venv\Scripts\python.exe -m tradingagents.desktop.reviews --workspace output/review-workspace record TRADE_ID notes.json --request-id MY_UNIQUE_REQUEST
.\.venv\Scripts\python.exe -m tradingagents.desktop.reviews --workspace output/review-workspace list --trade-id TRADE_ID --limit 20
.\.venv\Scripts\python.exe -m tradingagents.desktop.reviews --workspace output/review-workspace get REVIEW_ID
.\.venv\Scripts\python.exe -m tradingagents.desktop.reviews --workspace output/review-workspace draft REVIEW_ID definition.json
```

`notes.json` 直接包含上述四个复盘字段：

```json
{"research": null, "strategy": null, "execution": "尚未取得原计划，暂不能评价", "uncertainty": "单笔结果不能建立因果"}
```

`definition.json` 使用 `docs/strategy-knowledge.md` 中的策略定义格式；至少明确填写名称及一条规则的内容、出处、来源类型和定义状态。草案入口会强制转换为个人待定义规则，确认仍须补齐全部范围、时点及七类规则。命令输出 JSON，可复制 `strategy_id` 后在现有策略页面查找、编辑及确认。复盘列表不合并或覆盖旧意见；暂未提供复盘管理页面。
