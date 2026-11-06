# Decision Quality Implementation Plan

**Goal:** 修复复盘确认的事件输入、判断期限、条件执行与展示缺失。
**Architecture:** 扩展现有 AKShare 新闻检索和 GroundedReport；复用 Markdown 源链接和桌面桥。授权来自用户本轮“修复”，在当前工作区串行执行，不覆盖已有未提交工作。
**Tech Stack:** Python, Pydantic, LangGraph, pytest, local HTML/CSS/JS, Playwright, PyInstaller.

- [x] 新增 `tests/test_decision_quality.py`，覆盖最终计划完整性、失败后修复及原始分析师意见传递；扩充 `tests/test_akshare_news.py` 与 `tests/test_desktop_preview.py`，先确认新断言失败。
- [x] 在 `tradingagents/dataflows/vendors/akshare/news.py` 增加事件候选模式，复用主体、日期、去重；通过 `router.py`、`agents/tools.py`、`news_analyst.py`、`graph/setup.py` 接通确定性预取。候选先筛后限，发布日期不越界，覆盖不足不等于无事件。
- [x] 新增 `agents/decision.py` 统一计划模型、渲染与共同推理规则；在 `grounding.py` 增加仅最终经理要求的计划校验与一次修复。更新最终经理、研究经理和交易员，将原始报告及共同规则送达。
- [x] 在 `desktop/preview.py`、`static/app.js`、`app.css` 展示源链接计划字段；扩展传统 `schemas.py` 输出，以便其他市场和存量格式也能明确缺失项。历史正文和有效评级不变。
- [x] 跑上述测试及现有 grounded、新闻、结构输出和桌面测试；处理失败后跑全量离线 pytest。用 `desktop/browser_smoke.py` 检查旧报告与结构化新报告的展示与点击定位，保存截图。
- [x] 重新运行 `python -m PyInstaller --noconfirm desktop/desktop.spec`，检查打包文件并更新 macOS zip；记录已运行的验证与限制。
