# 策略时序核对

`StrategyTimingRegistry` 对已有条件检查做事后时序核对，绑定原检查和当时的已确认策略版本，另存不可变快照。它不生成订单、成交或收益，不改变策略验证状态。

## 显式时序契约

在策略定义中通过现有保存 API 添加可选 `timing` 对象，再确认新版本：

```json
{
  "observation": "completed_bar",
  "confirmation_delay_seconds": 60,
  "execution": "next_session_open",
  "price_basis": "raw",
  "price_unit": "CNY/share"
}
```

目前仅支持完成观测和下一交易时段开盘。延迟为 0–31536000 的整数秒，价格口径与单位必须明确。旧策略仍可使用原条件检查；没有契约时不能核对时序。桌面表单尚无专用编辑入口，不从自由文本推断契约。

观测时间取原条件检查实际采用的左右操作数观测；确认时间取检查的 `as_of`，必须不早于最后采用数据的 `available_at` 加确认延迟。未采用的未来修订不参与计算。全部人工规则或常数表达式没有观测证据时，返回数据不足。

## 调用者提供的证据

以下是合成示例，并非真实交易所日历或报价：

```json
{
  "calendar": {
    "market": "CN-A",
    "source": "synthetic calendar",
    "revision": "v1",
    "coverage_start": "2020-01-03T00:00:00+08:00",
    "coverage_end": "2020-01-07T00:00:00+08:00",
    "complete": true,
    "sessions": [
      {"open_at": "2020-01-03T09:30:00+08:00", "close_at": "2020-01-03T15:00:00+08:00"},
      {"open_at": "2020-01-06T09:30:00+08:00", "close_at": "2020-01-06T15:00:00+08:00"}
    ]
  },
  "execution_price": {
    "at": "2020-01-06T09:30:00+08:00",
    "available_at": "2020-01-06T09:30:01+08:00",
    "value": 11,
    "unit": "CNY/share",
    "price_basis": "raw",
    "source": "synthetic opening quote",
    "revision": "v1"
  }
}
```

日历覆盖区间左闭右开，必须声明完整且覆盖确认时间，才会寻找严格晚于确认时间的首个开盘。没有下一开盘时要求扩展证据，不猜测周末或节假日。确认时刻恰好开盘时，该开盘不能作为之后的执行时点。调用者需明确时段语义；若把午后开市作为独立时段，其开盘也会参与选择。

时段允许乱序，但必须完整落在覆盖区间内；重复、重叠、共享边界均拒绝。所有时间需包含时区，等价时刻按同一瞬间比较。证据最多 8 MiB、10000 个时段。

执行价格未知时用 `null`。价格必须为有限正数，可得时间不得早于价格时间，口径与单位需符合契约，价格时间须为确认后的下一开盘。当日收盘、确认同刻价格及下一开盘后的盘中报价均不能冒充下一开盘价。该价格仅作事后核对，绝不作为确认时已知的条件输入。

## API 与离线命令

桌面桥接提供 `assess_strategy_timing(check_id, evidence)`、`get_strategy_timing(audit_id)` 和 `strategy_timing_history(check_id)`。

关闭目标工作空间后运行：

```powershell
.\.venv\Scripts\python.exe -m tradingagents.desktop.strategy_timing --workspace ./output/dev-workspace assess CHECK_ID ./output/timing-evidence.json
.\.venv\Scripts\python.exe -m tradingagents.desktop.strategy_timing --workspace ./output/dev-workspace get AUDIT_ID
.\.venv\Scripts\python.exe -m tradingagents.desktop.strategy_timing --workspace ./output/dev-workspace history CHECK_ID
```

结果为 `consistent`（时序一致）、`inconsistent`（存在明确冲突）或 `insufficient_data`（证据不足）。冲突优先，但保留全部原因；结构无效的证据直接拒绝。原检查的人工、缺失、未实现和不满足状态保留在条件计数中，时序一致不代表整体策略满足。

快照按引擎版本、原检查内容和完整证据指纹标识。同一输入重试返回原快照；证据修订产生新快照，不覆盖历史。策略之后编辑也不影响已绑定版本。快照随工作空间数据库备份与恢复。

## 尚未覆盖

完成观测、日历完整性、报价及可得时间均是调用者声明，结果明确标记 `caller_supplied_unverified`，尚未与供应商档案核验。服务未核对停牌、涨跌停、流动性、T+1、数量、费用或滑点，也不提供自动数据接入、逐笔回测与真实试点验收。路线图 S03 整项仍未完成。

## 桌面入口

在策略版本详情中保存或选择一条条件检查，下面的“策略时序审计”显示该版本契约。已有契约时可粘贴上文格式的证据并保存；未定义契约时仅显示说明及历史查询，仍需通过策略 API 创建并确认新版本，桌面尚无契约编辑器。

结果分别展示时序一致、不一致和证据不足，以及观测、输入可得、确认、预期执行时刻。完整快照保留原条件计数、证据来源、修订、口径和限制。审计不覆盖原条件检查。

历史限定当前条件检查和策略版本；刷新窗口后先重新选择检查，再读取审计历史。当前窗口中切换检查或版本保留各自输入；提交和读取失败保留输入与已有结果，可重试。相同证据幂等保存。所有内容按文本转义，不执行证据中的 HTML。

离线真实 API 浏览器验收（临时工作空间，不访问行情或模型）：

```powershell
.\.venv\Scripts\python.exe desktop/strategy_timing_smoke.py --chromium-path "C:/Program Files/Google/Chrome/Application/chrome.exe"
```
