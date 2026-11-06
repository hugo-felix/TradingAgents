# StockSeek桌面工作台

StockSeek在当前 TradingAgents 引擎上提供本地桌面界面，面向 macOS 与 Windows。首页关注研究更新，股票档案按“研究摘要 / 事实与依据 / 我的判断 / 完整报告”组织信息。

## 从源码启动

Python 3.11 或更高版本，在仓库根目录运行：

```sh
pip install ".[desktop,a-shares]"
python -m tradingagents.desktop
```

也可使用安装后生成的 `tradingagents-desktop` 命令。当前仓库已有虚拟环境时：

```sh
.venv/bin/python -m tradingagents.desktop
```

Windows 对应 `\.venv\Scripts\python.exe -m tradingagents.desktop`。

可选启动参数：

```sh
python -m tradingagents.desktop --data-dir ./output/my-workspace --import-reports ./artifacts --env-file .env
```

`--data-dir` 为独立工作空间，默认 `~/.tradingagents/desktop`；也可设置 `TRADINGAGENTS_DESKTOP_DIR`。同一工作空间只允许一个应用实例。运行期间不要用其他程序改写其数据库。

## 使用

1. **自动读取已有报告**：启动后自动读取引擎结果目录（默认 `~/.tradingagents/logs`）、当前项目的 `artifacts/` 与 `reports/`，以及以前关联过的目录。从项目 `dist` 中通过 Finder 打开应用也能识别项目。每 15 秒检查新增或变更，无需逐次导入；没有变化的文件不会重复解析。其他位置的报告可在“应用设置 → 报告自动读取”中添加目录一次，之后持续检查。支持分章报告、事实附录与汇总 Markdown，原目录不修改，桌面中的个人判断和历史版本不会被覆盖。
2. **股票研究**：按名称、代码和评级检索；每次研究单独留档，同日可有多个版本。点击报告先看重点，引用按钮跳转原文；“来源事实与口径”保留数值、单位、期间和限定条件。
3. **我的判断**：按需展开表单，保存继续观察、研究意图或计划调整。与当时的报告版本关联，不改写模型评级，也不会修改实际持仓。
4. **持仓管理**：手工维护实际数量和每股成本，填写更新说明。无变化保存不新增记录，数量为 0 表示已清仓。当前不推算账户市值、收益率、现金和历史分红。
5. **应用设置**：选择服务商，填写该服务支持的快速/深度模型 ID。API Key 可写入系统凭据库，也可沿用环境变量。自定义地址只能填写不含凭据的 HTTP(S) 基础地址。CLI 的企业配置与订阅认证可继续使用。
6. **发起分析**：填写股票、日期和模型，运行独立 Python 工作进程。显示真实阶段和耗时，不编造百分比；可取消，失败可按原配置重试。完成后生成新研究版本，旧结果保留。模型调用会产生服务商费用。

输入 `601658` 可自动识别为“邮储银行”，并用于持仓、报告和历史记录显示。`601658`、`601658.SH`、`601658.SS` 与 `sh601658` 归为同一只股票，既有裸代码记录无需重新录入；名称匹配不会改写原始数量、成本或历史说明。内置名称表来自沪深交易所公开证券列表，2026-11-06 快照包含 5224 只股票，断网也能匹配。未覆盖的代码明确提示暂未识别，不由模型猜测名称。

窗口标题仅显示 `StockSeek`。下拉菜单使用统一主题，支持方向键、回车、Esc 和键盘跳转；持仓弹窗提供名称确认、单位和行内错误提示。备注可不填，仍以“手动维护持仓”留存操作来源；保存无变化的持仓不会新增记录，每股成本显示最多四位小数。

`⌘/Ctrl + K` 打开研究搜索；`⌘/Ctrl + N` 打开发起分析；股票页签支持左右方向键。返回会保留筛选、草稿与阅读位置。

首页只反映已有研究，尚未持续监测行情和公告。“暂时没有待查看研究”不能理解为市场没有新变化。没有对应结构化字段时，摘要显示缺失状态并提供原文，不新增模型调用替旧报告编写摘要。

## 本地文件与凭据

工作空间包括：

- `workspace.sqlite3`：不可变报告快照、索引、个人判断、持仓、关联的报告目录与文件变更签名。
- `runs/<run_id>/`：每次分析的报告及状态日志；取消或失败的目录不会发布为完成报告。
- `cache/` 与 `memory/`：引擎缓存与研究记忆。
- `workspace.lock`：操作系统管理的实例锁，进程结束自动释放。

关闭应用后复制整个工作空间可备份。API Key 使用 `TradingAgents.Desktop` 系统凭据库条目，与数据库分开；不进入页面返回值或报告。新增 API Key 不写入 `.env`。源码启动会读取启动目录的 `.env`，发行包不会内嵌 `.env`，可用 `--env-file` 显式指定。

## 构建桌面应用

在目标操作系统上分别构建，不能在 macOS 上直接产出 Windows 可执行文件：

```sh
pip install ".[desktop,a-shares]" "pyinstaller>=6,<7"
python -m PyInstaller --noconfirm desktop/desktop.spec
```

- macOS：`dist/StockSeek.app`，使用系统 WKWebView。本机产物为 Apple Silicon；Intel 需在对应环境构建验证。
- Windows x64：`dist/StockSeek/StockSeek.exe`。分发整个 `StockSeek` 文件夹，包含独立后台工作程序和 Python 运行时。需已安装 Microsoft Edge WebView2 Runtime；固定使用现代 Edge，不回退到 IE。
- 两个平台都直接加载本地静态资源，无需额外启动 HTTP 后台、外部数据库或安装 Python。
- `.github/workflows/desktop.yml` 提供手动及 PR 构建，上传产物但不自动发布。正式发行签名与 macOS 公证需要相应证书；当前是未公证的开发构建。

## 验证

```sh
python -m pytest tests/test_desktop_reports.py tests/test_desktop_tasks.py tests/test_desktop_launch.py -q
python desktop/frozen_smoke.py dist/StockSeek.app/Contents/MacOS/StockSeek
```

日常界面检查采用下方的无界面浏览器回归，不会打开桌面应用。原生验收仅手动执行，必须显式开启：

```sh
PYTHONPATH=. python desktop/ui_smoke.py --native --report-dir artifacts/600941_glm53flash_20261105
```

原生验收默认隐藏窗口，传入 `--show-window` 才显示，但仍会创建原生应用进程。它需要图形桌面会话，使用临时工作空间，不读写真实持仓或凭据、不调用模型；完成后强制结束验收进程，异常时由超时机制回收。

可选浏览器布局回归（测试工具不属于应用依赖）：

```sh
pip install playwright
playwright install chromium
PYTHONPATH=. python desktop/browser_smoke.py --report-dir artifacts/600941_glm53flash_20261105
```

需要用已有 Chromium 时传入 `--chromium-path /path/to/chrome`。截图写入 `output/desktop/`。

应用图标使用用户选定的牛熊 S 形原图，原文件保存在 `desktop/assets/source/stockseek-original.png`。打包使用 `desktop/assets/app.icns` 与 `app.ico`，侧栏使用透明底 `stockseek-mark.png`。需要重新生成时运行：

```sh
pip install pillow
python desktop/generate_icons.py
```

Pillow 只用于开发期的图标处理，不是桌面应用的运行依赖。脚本提取中心标志、统一居中与留白，保留牛熊构图，并生成 16–1024px 的图标资源。原图保持完整。

## 验收进程清理修复（2026-11-06）

旧原生验收脚本在断言通过后调用 `sys.exit`，仍可能等待 WebView 后端的非守护线程，导致多个 Python 验收实例留在桌面。现在原生验收需要 `--native` 显式开启，并在清理完成后结束整个测试进程；超时机制也负责回收。`tests/test_desktop_smoke_lifecycle.py` 覆盖线程未退出和缺少显式开启标记两种情况。

界面改为更紧凑的工具栏、研究列表与阅读分栏，减少教学说明、空卡片和重复提示；保留可展开的数据缺口与原文入口。新增统一 SVG 导航图标及独立应用图标。

## 自动报告库修复（2026-11-06）

启动与报告目录检查在后台进行。桌面自身 `runs/` 的完成结果仍由任务成功状态发布，自动检查不会把取消或未完成任务中的文件误发布。外部文件刚修改时等待短暂稳定期再读取；格式异常的报告单独记录并重试，不会阻塞其他报告。

如果把应用第一次直接移到 `/Applications` 等独立目录，且引擎使用非默认位置，应用不能推断任意磁盘路径；可在设置里关联项目报告目录一次。已经识别或关联的目录会保留，后续移动应用无需重复配置。

## 分维度研究速览（2026-11-06）

侧栏统一为今日关注、股票研究、持仓管理、历史记录，底部为应用设置；页面标题与顶部位置同步。

今日关注左侧仅保留股票、评级、首次/更新提示与日期，避免重复大段结论。选中研究后，右侧在剩余窗口高度内显示总体结论、技术面/基本面/消息面/情绪面重点和后续关注。中部内容可滚动，顶部股票身份与底部“打开研究 / 记录判断”操作保持可见；窄窗使用纵向布局。

各维度从明确的摘要/结论标签或结构化报告中提取原文，点击重点可定位原始段落。风险段落会保留可获得的趋势背景或支撑因素。缺少独立章节、摘要未提取、未评估、数据不足与校验失败分别显示；缺失不能解释成没有风险。长段落仅限制视觉行数，保留全部条件、否定及口径限定供原文查阅，不重新调用模型、不生成新的经济判断。

速览在读取时由已有快照派生，旧报告无需重新导入，也不会因此新增研究版本或覆盖个人记录。
