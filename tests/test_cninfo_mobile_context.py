"""Offline disclosure context regression tests; never invoke a model or network."""
from datetime import date

from tradingagents.dataflows.vendors import cninfo


def row(title, day='2026-08-14'):
    return {'title': title, 'day': date.fromisoformat(day), 'url': 'https://static.cninfo.com.cn/test.pdf'}


def test_default_six_selects_comparable_periods_before_duplicate_categories():
    entries = [row('2026年第三季度客户数据公告', '2026-10-20'),
               row('2026年第二季度客户数据公告', '2026-07-20'),
               row('2025年第三季度客户数据公告', '2025-10-20'),
               row('2026年半年度报告'), row('2025年半年度报告', '2025-08-14'),
               row('2026年中期A股分红派息实施公告'), row('2026年第三季度报告')]
    selected = cninfo._select(entries, 6)
    assert {r['title'] for r in selected} == {r['title'] for r in entries if '第二季度' not in r['title']}


def test_missing_context_is_explicit_and_other_issuers_are_not_parsed():
    disclosure = row('2024年半年度报告')
    output = cninfo._document(disclosure, ['旧版资本开支图表610584'], '600941')
    assert '自由现金流：未可靠提取' in output
    assert '增值税税目' in output and '未可靠提取' in output
    other = cninfo._document(disclosure, ['自由现金流 123'], '600000')
    assert '现金与分红口径' not in other


def test_disclosure_text_preserves_material_qualifications_and_units():
    # Minimal text-layout fixture: no downloaded filing or local PDF dependency.
    pages = [
        '公司营业收入达到人民币 5,380 亿元，同比下降 1.1%；归母利润人民币 789 亿元，同比下降 6.3%。\n'
        '同口径实现正增长。\n同口径指剔除增值税税目政策调整影响，下同。\n'
        '通信服务阶段承压，收入人民币 3,504 亿元，同比下降 5.7%。\n'
        '算力服务增势强劲，收入人民币 529 亿元，同比增长 14.0%。\n'
        '智能服务提能蓄势，收入人民币 493 亿元，同比增长 0.9%。',
        '公司主要会计数据和财务指标\n除特别注明外，金额单位为人民币百万元。',
        '2026 年上半年 2025 年上半年 变化\n'
        '经营活动产生的现金流量净额 114,854 83,832 37.0%\n'
        '自由现金流 53,869 25,460 111.6%',
        '向全体普通股股东拟宣派的中期股息合计人民币 544.26 亿元。',
        '应收账款自账单产生时计算账龄。\n(c) 坏账准备。',
        '报告期末主要资产受限情况：履约保证金。',
        '租赁负债 四(29)。资本开支图表610584',
    ]
    output = cninfo._document(row('2026年半年度报告'), pages, '600941')
    for text in ('5,380', '1.1%', '789', '6.3%', '同口径', '剔除增值税税目政策调整影响',
                 '53,869', '25,460', '114,854', '人民币百万元', '2025 年上半年',
                 '544.26', '拟宣派', '全体普通股', '并非已支付',
                 '3,504', '5.7%', '529', '14.0%', '493', '0.9%',
                 '账龄', '坏账', '受限', '租赁负债', '第7页', '2026H1',
                 '收入不等于利润', '不自动计算', '未可靠提取'):
        assert text in output, text
    assert '610584 亿元' not in output
    assert len(output) < 14000


def test_default_window_and_limit_allow_two_year_evidence():
    from inspect import signature
    parameters = signature(cninfo.get_company_disclosures).parameters
    assert parameters['look_back_days'].default == 730
    assert parameters['limit'].default == 6


def test_default_selection_does_not_fill_with_duplicate_period_categories():
    entries = [row('2026年第三季度客户数据公告'), row('2026年第二季度客户数据公告'),
               row('2026年第一季度客户数据公告'), row('2026年半年度报告'),
               row('2024年半年度报告'), row('2026年第三季度报告')]
    selected = cninfo._select(entries, 6)
    assert '2026年第一季度客户数据公告' not in [r['title'] for r in selected]
    assert '2024年半年度报告' not in [r['title'] for r in selected]
