"""Financial-sector screening presets, not a regulatory compliance assessment."""
from __future__ import annotations

import math
from datetime import date

INDICATORS = 'stock_financial_analysis_indicator_em'
MAX_REPORT_AGE_DAYS = 270


def financial_sector(sector):
    for word in ('银行', '保险', '证券', '多元金融'):
        if word in sector:
            return word
    return None


def _date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def financial_checks(rows, sector, asof, official=()):
    """Use one disclosed, fresh reporting period; never backfill individual ratios.

    Insurance's ambiguous SOLVENCY_AR is reference-only: neither the insured entity
    nor core/comprehensive scope is identified by this endpoint.
    """
    kind = financial_sector(sector)
    eligible = []
    for row in rows:
        report, notice = _date(row.get('REPORT_DATE')), _date(row.get('NOTICE_DATE'))
        update = _date(row.get('UPDATE_DATE'))
        if (report and notice and report <= notice <= asof
                and (update is None or update <= asof)):
            eligible.append((report, notice, row))
    latest = max(eligible, key=lambda item: (item[0], item[1]), default=None)
    fresh = latest is not None and 0 <= (asof - latest[0]).days <= MAX_REPORT_AGE_DAYS
    raw = latest[2] if fresh else {}
    report = latest[0].isoformat() if latest else None
    notice = latest[1].isoformat() if latest else None
    context = (f'报告期 {report}，公告 {notice}' if latest else '无筛选日以前已公告的报告')
    if latest and not fresh:
        context += f'；报告超过{MAX_REPORT_AGE_DAYS}天'
    metrics = {'financial_indicator_basis': 'sector_v1', 'financial_sector': kind,
               'financial_report_date': report, 'financial_notice_date': notice,
               'financial_indicator_fields': {}}
    checks = []

    def add(label, condition, detail, required=True):
        checks.append({'label': label, 'status': 'pending' if condition is None else
                       'pass' if condition else 'fail', 'detail': detail,
                       'required': required})

    def field(key, label, lower=None, upper=None, unit='%'):
        value = _number(raw.get(key))
        evidence = [r for r in official if fresh and r['report_date'] == report
                    and _date(r['notice_date']) <= asof
                    and r['scope'] == 'listed_entity' and key in r['fields']]
        values = {r['fields'][key] for r in evidence}
        conflict = len(values) > 1 or (value is not None and values
                                     and any(abs(value - v) > .01 for v in values))
        source = f'AKShare {INDICATORS}/{key}'
        if evidence:
            source += '；公告 ' + '；'.join(
                f'{r["entity"]} {r["source_url"]} 第{r["page"]}页，披露 {r["notice_date"]}'
                for r in evidence)
            if value is None and not conflict:
                value = next(iter(values))
        if conflict:
            value = None
        # Negative regulatory ratios are possible and must fail, not disappear.
        metrics['financial_indicator_fields'][key] = {
            'label': label, 'value': value, 'unit': unit,
            'report_date': report, 'notice_date': notice,
            'source': source,
        }
        good = (value >= 0 and (lower is None or value >= lower)
                and (upper is None or value <= upper)) if value is not None else None
        threshold = (f'≥{lower:g}{unit}' if lower is not None else '')
        threshold += f'≤{upper:g}{unit}' if upper is not None else ''
        add(label + threshold, good,
            f'{"缺失" if value is None else f"{value:g}{unit}"}；{context}；'
            f'{source}；' + ('来源数值冲突，需复核；' if conflict else '')
            + '研究预设，非监管合规认定')

    if kind == '银行':
        field('NONPERLOAN', '不良贷款率', upper=2)
        field('BLDKBBL', '拨备覆盖率', lower=150)
        field('HXYJBCZL', '核心一级资本充足率', lower=9)
        field('NEWCAPITALADER', '资本充足率', lower=12)
    elif kind == '证券':
        field('RISK_COVERAGE', '风险覆盖率', lower=120)
        field('CAPITAL_LEVERAGE_RATIO', '资本杠杆率', lower=9.6)
        field('LIQUIDITY_COVERAGE_RATIO', '流动性覆盖率', lower=120)
        field('NET_FUNDING_RATIO', '净稳定资金率', lower=120)
    elif kind == '保险':
        # Reference-only observations can use a different period, explicitly
        # dated per field. They never participate in pass/fail ratios.
        for key, label in [('SOLVENCY_AR', '偿付能力充足率（对象及口径未确认）'),
                           ('TOTAL_ROI', '总投资收益率（参考）'),
                           ('NET_ROI', '净投资收益率（参考）')]:
            observations = [item for item in eligible
                            if (asof - item[0]).days <= MAX_REPORT_AGE_DAYS
                            and _number(item[2].get(key)) is not None]
            observation = max(observations, key=lambda item: (item[0], item[1]), default=None)
            value = _number(observation[2][key]) if observation else None
            metrics['financial_indicator_fields'][key] = {
                'label': label, 'value': value, 'unit': '%',
                'report_date': observation[0].isoformat() if observation else None,
                'notice_date': observation[1].isoformat() if observation else None,
            }
            if observation:
                add(label, True, f'{value:g}%；报告期 {observation[0]}，公告 {observation[1]}；'
                    f'AKShare {INDICATORS}/{key}；仅为已取得参考值，不证明风险合格', False)
        solvency = metrics['financial_indicator_fields']['SOLVENCY_AR']
        value = solvency['value']
        insurance = [r for r in official if r['scope'] == 'listed_entity'
                     and _date(r['notice_date']) <= asof
                     and 0 <= (asof - _date(r['report_date'])).days <= MAX_REPORT_AGE_DAYS]
        capital = [r for r in insurance if {'CORE_SOLVENCY', 'COMPREHENSIVE_SOLVENCY'}
                   <= r['fields'].keys() and (report is None or r['report_date'] >= report)]
        latest_capital = max(capital, key=lambda r: (r['report_date'], r['notice_date']),
                             default=None)
        add('保险偿付能力口径确认', None if not latest_capital else True,
            (f'公告已明确上市主体 {latest_capital["entity"]} 的核心/综合口径，见下列指标'
             if latest_capital else f'SOLVENCY_AR={value if value is not None else "缺失"}%；'
            f'报告期 {solvency["report_date"]}，公告 {solvency["notice_date"]}；'
            '接口未标明集团/子公司、核心/综合口径，不能代替核心≥50%、综合≥100%的核验'))
        if latest_capital:
            r = latest_capital
            same_period = [e for e in capital if e['report_date'] == r['report_date']]
            for key, label, threshold in [('CORE_SOLVENCY', '保险核心偿付能力充足率', 50),
                                           ('COMPREHENSIVE_SOLVENCY', '保险综合偿付能力充足率', 100)]:
                values = {e['fields'][key] for e in same_period}
                value = r['fields'][key] if len(values) == 1 else None
                metrics['financial_indicator_fields'][key] = {
                    'label': label, 'value': value, 'unit': '%',
                    'report_date': r['report_date'], 'notice_date': r['notice_date'],
                    'entity': r['entity'], 'source_url': r['source_url'], 'page': r['page']}
                add(label + f'≥{threshold}%', value >= threshold if value is not None else None,
                    f'{value if value is not None else "来源冲突"}%；{r["entity"]}；'
                    f'报告期 {r["report_date"]}，披露 {r["notice_date"]}；'
                    f'{r["source_url"]} 第{r["page"]}页；研究预设，非完整监管合规认定')
        ratings = [r for r in insurance if 'RISK_RATING' in r['fields']]
        rating = max(ratings, key=lambda r: (r['report_date'], r['notice_date']), default=None)
        rating_values = {r['fields']['RISK_RATING'] for r in ratings
                         if rating and r['report_date'] == rating['report_date']}
        add('保险风险综合评级≥B类', None if not rating or len(rating_values) != 1 else
            rating['fields']['RISK_RATING'] in ('AAA', 'AA', 'A', 'BBB', 'BB', 'B'),
            (f'{rating["fields"]["RISK_RATING"] if len(rating_values) == 1 else "来源冲突"}；'
             f'{rating["entity"]}；评级期间 '
             f'{rating["report_date"]}，披露 {rating["notice_date"]}；'
             f'{rating["source_url"]} 第{rating["page"]}页；不代表后续季度评级'
             if rating else '缺少可对应上市主体、注明评级期间的官方风险综合评级'))
        add('保险投资资产质量', None,
            '仍需核验投资资产减值、逾期及集中度；偿付能力、风险评级或投资收益率不能代替资产质量')
    else:
        add('多元金融适用监管指标', None, '需按具体业务和监管主体核验资本、流动性及资产质量')
    status = ('fail' if any(c['status'] == 'fail' for c in checks) else
              'pending' if any(c['status'] == 'pending' for c in checks) else 'pass')
    add('银行资产质量与资本充足' if kind == '银行' else '金融行业专属指标',
        None if status == 'pending' else status == 'pass',
        f'{context}；详见行业逐项指标；不使用普通企业现金流/负债率规则')
    return metrics, checks
