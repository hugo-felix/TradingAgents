# 项目协作指南

本文件适用于整个仓库。修改前阅读相关实现和测试；若子目录存在更具体的 `AGENTS.md`，同时遵循其约定。

## 项目与目录

- TradingAgents 是基于 LangGraph 的多智能体金融研究引擎，StockSeek 是其本地桌面工作台。
- `tradingagents/agents/`：分析师、研究员、交易与风险管理角色。
- `tradingagents/graph/`：工作流编排与执行。
- `tradingagents/dataflows/`：数据供应商接入、路由与数据处理。
- `tradingagents/llm_clients/`：模型服务商客户端与适配。
- `tradingagents/memory/`：研究记忆；`backtest.py`、`portfolio.py`、`reporting.py` 等负责回测、组合与报告。
- `tradingagents/desktop/`：桌面入口、Python API、SQLite 存储、报告库及后台任务；`static/` 为 HTML、CSS、JavaScript 界面，`data/` 为内置数据。
- `desktop/`：打包配置、图标资源及桌面验收工具。
- `cli/`：命令行入口；`tests/`：回归测试；`docs/desktop.md`：桌面使用、数据目录与打包说明。
- `pyproject.toml` 是依赖、测试与代码风格配置依据；CI 定义在 `.github/workflows/`。

## 环境与常用命令

需要 Python 3.11 或更高版本。优先使用现有 `.venv`，从仓库根目录运行。以下为 Windows PowerShell 命令；macOS/Linux 对应解释器为 `.venv/bin/python`。

```powershell
# 环境缺少依赖时安装；不要为日常启动重复安装
.\.venv\Scripts\python.exe -m pip install -e ".[dev,desktop,a-shares]"

# 启动 StockSeek：脚本使用项目 data/desktop 工作空间及根目录 .env
.\start-desktop.cmd

# 使用隔离工作空间启动，适合开发检查
.\.venv\Scripts\python.exe -m tradingagents.desktop --data-dir ./output/dev-workspace --env-file .env

# 启动 CLI
.\.venv\Scripts\python.exe -m cli.main

# 默认测试排除 integration
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .

# 桌面核心回归
.\.venv\Scripts\python.exe -m pytest tests/test_desktop_reports.py tests/test_desktop_tasks.py tests/test_desktop_launch.py -q
```

直接运行桌面模块且未指定目录时，工作空间默认位于 `~/.tradingagents/desktop`，也可由 `TRADINGAGENTS_DESKTOP_DIR` 指定。同一工作空间只允许一个应用实例。

## 修改约定

- 保持变更聚焦，保留用户已有修改，不顺带重构或全仓格式化。
- Python 使用四空格缩进，遵循现有命名、类型注解与模块风格；Ruff 目标为 Python 3.11，行宽配置为 100。
- 数据供应商调用归入 `dataflows/`；`yfinance`、`akshare` 不应由其他业务层直接导入。供应商异常按已有 `VendorError` 体系处理，不能把服务故障描述成市场事实。相关约束见 `tests/test_layering.py`。
- 新增模型或服务商能力时，检查现有客户端、注册配置和测试，避免在桌面界面另建一套配置逻辑。
- StockSeek 直接加载本地静态资源，通过现有 Python API 交互。沿用现有架构；依赖变化需同步 `pyproject.toml` 及必要的打包资源配置。
- 修改用户行为、启动参数或数据格式时，同步相关文档。文本以 UTF-8 读写，确保中文显示正常。

## 数据与研究语义

- 保持按分析日期可获得数据的约束：不得把未来公告、新闻、未完成行情或后续研究记忆泄漏到历史分析与回测。
- 保留事实的来源、日期、单位、期间和限定条件；缺失、未评估、数据不足与校验失败应明确区分，不能推断为零值或没有风险。
- 报告摘要应能定位到原文。对已有报告的展示和摘要提取不得悄悄新增模型调用、编造判断或覆盖原始报告快照。
- 个人判断、模型评级和实际持仓分别保存。研究导入、股票名称识别及界面展示不能改写用户数量、成本或历史说明。
- 后台分析仅在成功完成后发布研究结果；取消或失败的任务不能作为完成报告导入。保留既有版本与任务隔离。

## 本地状态与凭据

- 不将 `.env`、`.env.enterprise`、API Key、认证令牌、系统凭据或真实研究数据写入代码、日志、测试快照与提交。
- 需要配置示例时参考 `.env.example`、`.env.enterprise.example`，避免输出实际密钥内容。
- 桌面新增 API Key 使用现有系统凭据库流程，不返回前端或写入报告；发行包不内嵌 `.env`。
- 不直接改写运行中工作空间的 `workspace.sqlite3`，不删除用户的持仓、报告、缓存或记忆来解决开发问题。需要测试数据时使用临时目录或 `output/` 下独立工作空间。
- 不批量结束 Python 进程；启动或验收后只清理本次创建且可确认归属的进程。

## 验证与交付

- 先运行与变更相关的测试。业务逻辑或缺陷修复应补充有意义的回归覆盖；纯文档修改只需核对内容和差异。
- Python 代码修改完成后运行 Ruff；跨模块行为变更运行默认测试套件。CI 在 Python 3.11–3.14 上执行测试，并包含非 UTC 时区检查。
- `integration` 测试仅在用户要求时显式使用 `-m integration` 执行。日常验证使用 mock 或现有离线数据，避免触发付费模型分析。
- 界面布局优先使用 `desktop/browser_smoke.py`；参数及可选 Playwright 安装方式见 `docs/desktop.md`。原生验收 `desktop/ui_smoke.py` 需显式 `--native`，使用临时工作空间并确保退出后回收进程。
- 桌面打包仅在目标操作系统执行，配置入口为 `desktop/desktop.spec`；Windows 依赖 WebView2 Runtime。仅在涉及打包或用户要求时构建。
- 交付时简要说明修改内容、实际执行的验证和未完成的检查；不要把未运行的测试表述为通过。
