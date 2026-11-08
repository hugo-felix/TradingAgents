"""Dated, manually verified public-disclosure supplements; no network on read."""
from __future__ import annotations

import copy
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from tradingagents.dataflows.dividend_financials import (
    _date,
    _number,
    financial_checks,
    financial_sector,
)

FILENAME = 'dividend-official-evidence.json'
RATIOS = {'NONPERLOAN', 'BLDKBBL', 'HXYJBCZL', 'NEWCAPITALADER',
          'RISK_COVERAGE', 'CAPITAL_LEVERAGE_RATIO', 'LIQUIDITY_COVERAGE_RATIO',
          'NET_FUNDING_RATIO', 'CORE_SOLVENCY', 'COMPREHENSIVE_SOLVENCY'}


def load_evidence(path, asof):
    """Reject malformed files visibly; exclude disclosures unavailable as of the snapshot."""
    if path is None or not Path(path).exists():
        return [], None
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
        if payload.get('schema_version') != 1 or not isinstance(payload.get('records'), list):
            raise ValueError('schema_version/records 无效')
        records = []
        for raw in payload['records']:
            r = copy.deepcopy(raw)
            if (not isinstance(r, dict) or not re.fullmatch(r'\d{6}', r.get('code', ''))
                    or r.get('scope') != 'listed_entity'
                    or not isinstance(r.get('entity'), str) or not r['entity'].strip()
                    or type(r.get('page')) is not int or r['page'] < 1):
                raise ValueError('代码、主体、口径或页码无效')
            source = urlsplit(r.get('source_url', ''))
            if source.scheme != 'https' or not source.hostname or source.username or source.password:
                raise ValueError('公告必须使用 HTTPS 来源地址')
            report, notice = _date(r.get('report_date')), _date(r.get('notice_date'))
            if not report or not notice or report > notice:
                raise ValueError('报告/披露日期无效')
            fields = r.get('fields')
            if not isinstance(fields, dict) or not fields:
                raise ValueError('公告字段为空')
            for key, value in fields.items():
                if key in RATIOS:
                    if isinstance(value, bool) or _number(value) is None or r.get('unit') != '%':
                        raise ValueError('比例必须为有限数值，单位必须为 %')
                    fields[key] = _number(value)
                elif key == 'RISK_RATING':
                    if value not in ('AAA', 'AA', 'A', 'BBB', 'BB', 'B', 'C', 'D'):
                        raise ValueError('风险评级无效')
                elif key == 'A_LISTING_DATE':
                    if not _date(value):
                        raise ValueError('A股上市日期无效')
                else:
                    raise ValueError(f'不支持的公告字段：{key}')
            if notice <= asof:
                records.append(r)
        return records, None
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return [], f'公告补充证据未应用：{exc}'


def apply_evidence(row, records, asof):
    """Recompute financial checks from the original API observations on every projection."""
    selected = [r for r in records if r['code'] == row['code']]
    m = row['metrics']
    if financial_sector(row['sector']) and m.get('financial_indicator_basis'):
        # Keep original packets separate so deleting/expiring supplements cannot
        # leave an old official value masquerading as an API observation.
        base = m.setdefault('financial_api_observations', copy.deepcopy({
            key: m.get(key) for key in ('financial_indicator_fields', 'financial_report_date',
                                       'financial_notice_date')}))
        packets = {}
        report, notice = base.get('financial_report_date'), base.get('financial_notice_date')
        if report and notice:
            packets[(report, notice)] = {'REPORT_DATE': report, 'NOTICE_DATE': notice}
        for key, f in (base.get('financial_indicator_fields') or {}).items():
            period = (f.get('report_date'), f.get('notice_date'))
            if all(period):
                packets.setdefault(period, {'REPORT_DATE': period[0], 'NOTICE_DATE': period[1]})[
                    key] = f.get('value')
        _, old_checks = financial_checks(list(packets.values()), row['sector'], asof)
        values, checks = financial_checks(list(packets.values()), row['sector'], asof, selected)
        old_labels = {c['label'] for c in old_checks}
        # Repeated projections may contain dynamically added official ratio checks.
        old_labels.update({'保险核心偿付能力充足率≥50%', '保险综合偿付能力充足率≥100%',
                           '保险风险综合评级及投资资产质量'})
        row['checks'] = [c for c in row['checks'] if c['label'] not in old_labels] + checks
        m.update(values)
    listing = [r for r in selected if 'A_LISTING_DATE' in r['fields']]
    m.pop('a_listing_date', None)
    if listing:
        dates = {r['fields']['A_LISTING_DATE'] for r in listing}
        if len(dates) == 1:
            m['a_listing_date'] = next(iter(dates))
    row['official_evidence'] = selected
    statuses = [c['status'] for c in row['checks'] if c['required']]
    row['status'] = 'fail' if 'fail' in statuses else 'pending' if 'pending' in statuses else 'pass'
    for key in list(row.get('sources', {})):
        if key.startswith('official_'):
            del row['sources'][key]
    row.setdefault('sources', {}).update(
        {f'official_{i}': r['source_url'] for i, r in enumerate(selected)})


def listing_check(metrics, asof):
    listed = _date(metrics.get('a_listing_date'))
    if listed is None:
        return []
    anniversary = date(listed.year + 3, listed.month, min(listed.day, 28)
                       if listed.month == 2 else listed.day)
    return [{'label': 'A股上市满3年', 'required': True,
             'status': 'pass' if asof >= anniversary else 'fail',
             'detail': f'A股上市日期 {listed}；上市不足3年无法形成完整3年A股低波样本；'
                       'H股价格历史不替代A股；分红历史仍单独核验'}]
