# 数据接口盘点

核对日期：2026-11-09。基线为提交 `de77104`，包含本批 Yahoo 缺失值修复；不包含工作区原有尚未提交的通达信、估值和行情扩展。本文是代码能力盘点，不是供应商在线连通性、许可、报价或账户额度验收。没有请求外部服务或模型。

## 路由与覆盖

入口为 [router.py](../tradingagents/dataflows/router.py)，默认链来自 [default_config.py](../tradingagents/default_config.py)。方法级配置覆盖分类级配置；显式配置只在所选、且实现该方法的供应商之间依次回退。`default` 才采用全部已注册实现。分类默认链不意味着每个方法都由链中每个供应商实现。

| 方法 | 已注册实现 | 主要覆盖和限制 |
| --- | --- | --- |
| `get_stock_data` | yfinance、alpha_vantage | 日线行情；证券是否覆盖由供应商回答，不保证全市场、退市或历史成分覆盖 |
| `get_indicators` | yfinance、alpha_vantage | 技术指标；Yahoo 使用共享 OHLCV 和 stockstats，不能直接充当逐笔成交数据 |
| `get_fundamentals` | yfinance、alpha_vantage | 当前公司概况；没有可靠历史快照时禁止用于历史分析 |
| `get_balance_sheet`、`get_cashflow`、`get_income_statement` | akshare、sec_edgar、yfinance、alpha_vantage | AKShare 限交易所明确的 A 股；SEC 限其披露公司；历史可用性见下表 |
| `get_news` | akshare、yfinance、alpha_vantage | 日期窗口内的供应商新闻样本，不承诺完整历史新闻库 |
| `get_event_candidates` | akshare | 新闻标题中的候选事件；需要核实，不能当作已确认日历 |
| `get_global_news` | yfinance、alpha_vantage | 综合新闻入口；样本覆盖不能代表所有市场事件 |
| `get_insider_transactions` | yfinance、alpha_vantage | 供应商内部人交易记录；需遵守各实现的日期/公开时间限制 |
| `get_macro_indicators` | akshare、fred | 国内系列及 FRED 系列；系列名称、单位和发布口径不同 |
| `get_company_disclosures` | cninfo | 巨潮 A 股公告及有限 PDF 正文提取 |
| `get_dividends` | akshare | A 股分红方案，不等于实际账户到账记录 |
| `get_company_operating_history` | china_mobile | 中国移动官方经营资料的特定解析器，不是通用公司经营数据库 |
| `get_valuation_comparison` | akshare | PE/PB 历史描述与有限同业比较；不是历史时点估值库 |
| `get_prediction_markets` | polymarket | Gamma 当前开放事件市场隐含概率；不是确定预测或个股估值 |

默认行情、指标为 Yahoo；财务为 AKShare → SEC → Yahoo；新闻为 AKShare → Yahoo；宏观为 AKShare → FRED；公告分类为 CNInfo → AKShare → China Mobile（按方法筛选）；估值为 AKShare；预测市场为 Polymarket。Alpha Vantage 已注册但不在这些默认链中，使用前需显式配置。

## 凭据、费用与请求边界

这里只记录凭据**名称**与代码设置。代码未建立供应商账单、订阅计划、调用额度或费用计量；不需要 Key 不代表免费、无限调用或允许再分发。实际授权、费用和限额需在选定试点及账户后另行核实，不在本文填写猜测报价。

| 实现与来源 | 配置要求 | 代码中的超时、重试与限流处理 |
| --- | --- | --- |
| [Yahoo](../tradingagents/dataflows/vendors/yahoo/common.py) / yfinance | 项目层无 Key；依赖 yfinance | `YFRateLimitError` 默认重试 3 次，间隔 2/4/8 秒；其他异常转 `VendorUnavailableError`。空答复可探测 Yahoo 主机（5 秒）。SDK 内部超时不由共享包装器统一设置 |
| [Alpha Vantage](../tradingagents/dataflows/vendors/alpha_vantage/common.py) | `ALPHA_VANTAGE_API_KEY`；可配置 entitlement | 请求 30 秒；识别限流/套餐提示为不可用、缺失/无效 Key 为未配置；没有共享自动重试或全局速率调度 |
| [AKShare](../tradingagents/dataflows/vendors/akshare/common.py) / Eastmoney 等上游 | 可选 `a-shares` 依赖；共享层无 Key | 动态调用 SDK，验证 DataFrame；异常包装为不可用。共享层没有强制超时、退避或每源限速，不能假定 SDK 请求均有上限 |
| [SEC EDGAR](../tradingagents/dataflows/vendors/sec_edgar.py) | `SEC_EDGAR_USER_AGENT`；代码有默认标识，使用者应配置可联系标识 | 30 秒；异常及坏 JSON 转不可用；没有共享退避或 SEC 请求速率调度 |
| [FRED](../tradingagents/dataflows/vendors/fred.py) | `FRED_API_KEY` | 30 秒；HTTP 错误及服务错误不得视为宏观零值；没有自动重试/配额计量 |
| [CNInfo](../tradingagents/dataflows/vendors/cninfo.py) | 无项目 Key；PDF 需 pypdf | 公告 GET/POST 为连接 10 秒、读取 20 秒；查询最多 5 页；PDF 边界见下文；没有统一自动重试 |
| [China Mobile](../tradingagents/dataflows/vendors/china_mobile.py) | 无项目 Key；PDF 需 pypdf | 仅受限官方 HTTPS 来源；PDF 传输与解析有边界；没有通用限速调度 |
| [Polymarket](../tradingagents/dataflows/vendors/polymarket.py) | 公共 Gamma，无项目 Key | 30 秒；网络失败返回明确不可用文字；未实现自动重试、缓存或历史概率拉取 |

带 Key 的共享 HTTP 请求使用 [net.py](../tradingagents/dataflows/net.py) 清理异常中的敏感请求信息；这不代表所有 SDK、外部工具和未来适配器自动具有同样保证。

## 缓存、时间与研究边界

| 路径 | 当前行为 | 使用限制 |
| --- | --- | --- |
| [Yahoo OHLCV](../tradingagents/dataflows/vendors/yahoo/ohlcv.py) | 按证券缓存 CSV，原子替换，不缓存空结果；当天文件可复用，当前日期请求缓存有效期 900 秒；共享指标路径下载从现在起向前五年的调整行情 | 请求历史区间不保证可从该缓存取到；不是不可变数据集或原始报价档案。剔除未完成日线，最新行陈旧超过 10 个自然日拒绝；该阈值不能替代交易所停牌/节假日数据库 |
| Yahoo 指标与核验快照 | 通用 OHLCV 默认仅按交易日顺序向后沿用已知价格；开头的价格缺口、所有缺失成交量均保留，不从未来行回填；核验快照显式 `fill_gaps=False` | 沿用价格不能当作当日实际报价，自动调整历史仍是供应商当前修订，不能直接作为真实成交价格；没有完整公司行动账本。直接日线请求和共享指标缓存不是同一获取路径 |
| [SEC](../tradingagents/dataflows/vendors/sec_edgar.py) | ticker 映射和 companyfacts 缓存 24 小时；按 `filed <= as_of_date` 选择已披露版本 | 保留期间、单位与所选披露信息；当前 ticker 映射不是历史股票池。缓存不是完整逐次获取档案 |
| FRED | 请求 observation 窗口及 as-of vintage，按芝加哥日期约束可请求的今天 | 观测日期和首次公开日期不同；vintage 查询不可替换成简单截断最新系列。仍无统一数据集身份或离线不可变快照 |
| AKShare 财务、宏观、分红、估值 | 缺完整修订档案，历史日期返回 withheld；今日请求保留来源、期间与具体限制 | 不得因表中有历史期间就绕过 withheld 用于历史回测；新闻是近期搜索样本，日期过滤也不能构成完整历史档案 |
| [CNInfo PDF](../tradingagents/dataflows/vendors/cninfo.py) | 公告按公开日过滤；URL 哈希的文本缓存；下载不跟随重定向；最多 20 MiB、200 页、30 万字符 | 缓存及公开日不是独立修订档案；截断明确标记，扫描页无 OCR，不能把提取失败当成没有披露 |
| China Mobile PDF | 官方 URL 白名单、哈希缓存，缓存有效期 6 小时；最多 20 MiB、200 页、30 万字符，限制输出长度 | 特定模板及资料覆盖，不保证所有期间或所有经营指标；需保留资料公开时间与单位 |
| [公共 PDF 提取](../tradingagents/dataflows/pdf.py) | 连接/读取 10/20 秒，传输检查 60 秒总界；页间检查总计 90 秒解析界 | 单次同步解析器/页调用不能被这些页间检查中断；不是硬进程超时 |
| Polymarket | 对历史日期直接 withheld，仅今日概率 | 当前成交量、概率、市场状态没有历史 vintage，不得回填历史分析 |

[market_session.py](../tradingagents/dataflows/market_session.py) 与 [symbols.py](../tradingagents/dataflows/symbols.py) 已提供市场日期/证券代码基础。它们不等于统一行情契约、完整交易日历、历史成分、停牌或退市数据库。

## 不经过上述通用路由的已有路径

- [Reddit RSS](../tradingagents/dataflows/vendors/reddit.py)：最近一周搜索，默认合并 subreddit 单次请求、100 条 feed、5 MiB 上限、10 秒超时；429 最多重试一次，尊重 `Retry-After`，否则约 60 秒加抖动。日期过滤与覆盖不足提示保留；不是历史帖子全集。可选 `screen` 回调由调用者提供，可能涉及额外模型成本，抓取器自身不计费或调度模型。
- [StockTwits](../tradingagents/dataflows/vendors/stocktwits.py)：公共 symbol stream，日期筛选、覆盖不足/网络失败分别提示；可选 `screen` 回调。无历史完整性保证、统一重试或响应字节上限，不能把讨论样本百分比视为总体民意。
- [Eastmoney attention/forum](../tradingagents/dataflows/vendors/eastmoney_sentiment.py)：仅 A 股、仅当前上下文，最多近期七日样本；历史请求拒绝。连接/读取 5/10 秒、2 MB 上限、调用总期限检查、禁止重定向、不重试；排名、互动数是当前快照。
- [dividend_screen.py](../tradingagents/dataflows/dividend_screen.py)：独立股息筛选数据入口，包装 AKShare worker，非空行缓存 24 小时；默认子进程调用限时 50 秒，失败有短间隔重试；`direct` 调用不能获得子进程硬超时保护。腾讯报价单独 15 秒请求。交易日、交易所名单和股息资料由该路径整理；当前名单不代表历史成分。不能将筛选结果当成确定交易指令。
- [dividend_official.py](../tradingagents/dataflows/dividend_official.py)：读取本地官方证据清单，与财务/分红资料核对；不是联网自动发现与完整历史数据库。证据和真实研究文件不提交仓库。[dividend_probe.py](../tradingagents/dataflows/dividend_probe.py) 为独立诊断工具，不属于日常离线回归。
- 本地书籍登记与解析在 `desktop/books.py`、`desktop/book_text.py`，不是市场供应商接口；不会自动扩展行情覆盖或产生市场事实。

## 失败契约与下一步

[errors.py](../tradingagents/dataflows/errors.py) 区分 `VendorUnavailableError`（请求/限流等故障）、`VendorNotConfiguredError`（未配置）和 `NoMarketDataError`（无可用/陈旧数据）。路由任一供应商成功即返回；全链失败时，服务故障优先返回 `DATA_UNAVAILABLE`；干净无数据返回 `NO_DATA_AVAILABLE`，不会填零。未分类异常与干净无数据混合也按故障处理；未配置与无数据混合仍可返回无数据并记录配置错误。没有可用结果且只剩真实异常时，核心类别抛出，宏观/公告/估值/预测等可选类别降级。返回 sentinel 或 withheld 文字不会继续寻找另一个供应商，调用者必须保留其中的限制。

[facts.py](../tradingagents/dataflows/facts.py) 的 `Fact` 已区分数值/缺失/未核验，保留单位、期间、口径、币种、限定条件和来源；只有部分适配器产生事实，纯文本结果不能自动获得结构化字段。当前没有统一的事实发生日、首次公开日、获取日及修订身份，也没有统一的数据集清单、缓存命中/源切换结构化状态。

后续优先扩展上述适配层：B02 统一行情及单位契约；B03 按实际试点核对个股/基准/行业覆盖；B04 追加不可变修订与可得时间；B05 核对历史股票池/停牌/退市；B06 将缓存、陈旧及源切换状态暴露给界面；B07 明确原始/调整价格和公司行动。本文只完成 B01 盘点，不代表 B02–B07 或供应商线上验收完成。

离线证据入口：`tests/test_vendor_routing.py`、`test_vendor_errors.py`、`test_ohlcv_cache_freshness.py`、`test_yfinance_stale_ohlcv_guard.py`、`test_sec_edgar.py`、`test_fred.py`、`test_akshare_*.py`、`test_cninfo.py`、`test_polymarket.py`、`test_reddit_fallback.py`、`test_stocktwits_resilience.py`、`test_eastmoney_sentiment.py`。这些 mock/离线回归不证明实时 SLA 或账户权限。
