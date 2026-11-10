# 离线条件跟踪与防抖

`ConditionTrackingRegistry` 对用户主动提交的不可变条件检查做顺序跟踪。计划绑定确切策略版本、条件组与零起始序号、证券、市场、周期、价格口径、来源、已有序列单位及检查引擎。来源或版本变化须另建计划；数据修订版本可以变化，但同一观测时刻的修订不能增加连续计数。

计划由具名用户明确设置连续次数（1–100）和含时区的到期时间，起始时间取种子检查的 `as_of`。种子仅作绑定，创建不会自动评估；可显式提交种子作为第一次记录。计划不可变，相同配置幂等创建，改变配置另建计划。当前没有提前取消或自动过期任务，到期状态在提交检查时计算。

## 判定语义

- 所选单条条件须为 `met`，所有指标操作数均配置 `max_age_seconds` 且为 `within_limit`，才可累积。常量不参与观测计数；全部是常量时暂停。人工、未实现、校验失败、数据不足、过期或新鲜度未评估都暂停并清零计数。
- 每个指标操作数最新采用的观测时间都须严格晚于上次计入的水位。同一时刻的重复数据、不同检查时刻或修订不能冒充独立观测。等价时区表示按实际瞬间比较。
- 数据仍满足但尚无独立新观测时，返回 `waiting_for_observation`，保留计数，不命中。观测倒退则暂停并清零，保留原水位。暂停后重新提供原观测也不能重新计数。
- `not_met` 显式清零，即使是同一观测的失败修订。之后需重新积累独立观测。
- 首次达到阈值时返回 `triggered`，且 `hit=true`。继续满足为 `sustained`、`hit=false`，直到不满足或暂停后重新积累。计数到阈值封顶。
- 起始时间包含在范围内，到期时间不包含；`as_of >= expires_at` 返回 `expired`、清零且不命中。过期不把该条件解释为市场不满足。

`observing` 表示积累中，`paused` 的 `reason` 保留缺失、新鲜度、人工或回退原因。历史按追加顺序倒序返回，每条保存完整检查、结果、水位与前一事件标识。新检查时间须严格递增；早于起始、同一时刻不同检查或乱序提交拒绝且不写历史。已保存检查在任意后续时间重试均返回原记录，避免重复触发。读状态与追加记录处于同一 SQLite 写事务，并发请求和失败重试不跳过计数。记录随数据库备份、恢复。

“连续”仅指用户提交序列，不证明行情、交易日或巡检覆盖完整。数据来源、可得时间和完成观测均未经供应商核验。单条条件命中不代表整体策略通过、可成交或交易建议。当前没有行情接入、调度、消息发送或持仓修改；真实计划试点尚未验收，M08 整项仍未完成。

## 桌面使用

1. 在“策略”中打开确切版本，读取条件检查历史并选择一项检查。在下方“条件跟踪”选择一条条件，填写连续次数、含时区的到期时刻和创建人，明确创建计划。创建不会自动提交种子检查。
2. 可以读取本版本已有计划并选择一项；其他策略或版本的计划不混入列表。计划保留种子、上下文和完整不可变配置。更改配置须创建另一计划。
3. 选择当前条件检查后，点击“将当前检查提交给此计划”进行一次计算。结果展示计数、命中或暂停原因及完整检查证据；重复提交原检查返回原事件，不增加计数。
4. 点击“读取 / 刷新此计划历史”读取完整历史。最近状态取已读取历史中的最新事件；重试旧检查不会把最近状态倒退成旧结果。未读取历史时不能据此推断当前状态。点击任一历史记录查看原始条件、来源、观测、水位及前序链。

同一窗口按策略版本保留表单及选择；请求等待期间切换版本，返回后可恢复对应结果。API 失败保留输入及已有证据，方便重试。刷新窗口后，重新读取计划及历史；未保存输入不会保留。计划到期按所提交检查的 `as_of` 计算，界面不把历史计划展示为此刻仍有效，也不自动巡检或发送通知。

可选离线浏览器验收使用临时工作空间与合成数据，不调用模型或行情：

```powershell
.\.venv\Scripts\python.exe desktop/condition_tracking_smoke.py --chromium-path "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

## API

桌面桥接沿用 `{ok, data/error}` 返回：

```text
create_condition_tracking(check_id, group, index, required_consecutive, expires_at, created_by)
get_condition_tracking(plan_id)
list_condition_tracking()
evaluate_condition_tracking(plan_id, check_id)
condition_tracking_history(plan_id)
```

底层注册表对应 `create`、`get`、`list`、`evaluate`、`history`。跟踪不会改变原检查、策略验证状态或实际操作记录。

## 离线命令

关闭目标工作空间后运行。以下次数与期限仅为合成使用示例，不是策略参数建议：

```powershell
.\.venv\Scripts\python.exe -m tradingagents.desktop.condition_tracking --workspace ./output/dev-workspace create CHECK_ID entry 0 --required-consecutive 2 --expires-at "2020-01-20T16:00:00+08:00" --created-by "测试人"
.\.venv\Scripts\python.exe -m tradingagents.desktop.condition_tracking --workspace ./output/dev-workspace list
.\.venv\Scripts\python.exe -m tradingagents.desktop.condition_tracking --workspace ./output/dev-workspace get PLAN_ID
.\.venv\Scripts\python.exe -m tradingagents.desktop.condition_tracking --workspace ./output/dev-workspace evaluate PLAN_ID CHECK_ID
.\.venv\Scripts\python.exe -m tradingagents.desktop.condition_tracking --workspace ./output/dev-workspace history PLAN_ID
```

命令拒绝缺失数据库、恢复未完成或正在使用的工作空间；不创建替代空工作空间。
