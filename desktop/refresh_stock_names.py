"""Refresh the bundled A-share display-name snapshot from exchange lists.

Development utility: requires requests, pandas and openpyxl. No account data.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pandas as pd
import requests

SSE = 'https://query.sse.com.cn/sseQuery/commonQuery.do'
SZSE = 'https://www.szse.cn/api/report/ShowReport'
BSE = 'https://www.bse.cn/nqxxController/nqxxCnzq.do'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.sse.com.cn/assortment/stock/list/share/'}


def shanghai(kind):
    params = {'STOCK_TYPE': kind, 'sqlId': 'COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L',
              'COMPANY_STATUS': '2,4,5,7,8', 'type': 'inParams', 'isPagination': 'true',
              'pageHelp.pageSize': '10000', 'pageHelp.pageNo': '1'}
    response = requests.get(SSE, params=params, headers=HEADERS, timeout=15)
    response.raise_for_status()
    return {row['A_STOCK_CODE'] + '.SS': row['SEC_NAME_CN'] for row in response.json()['result']
            if row.get('A_STOCK_CODE') and row.get('SEC_NAME_CN')}


def shenzhen():
    response = requests.get(SZSE, params={'SHOWTYPE': 'xlsx', 'CATALOGID': '1110', 'TABKEY': 'tab1',
                                          'random': '0.6935816432433362'},
                            headers={**HEADERS, 'Referer': 'https://www.szse.cn/market/product/stock/list/index.html'}, timeout=20)
    response.raise_for_status()
    table = pd.read_excel(BytesIO(response.content), dtype=str)
    return {str(row['A股代码']).zfill(6) + '.SZ': str(row['A股简称'])
            for _, row in table.iterrows() if pd.notna(row['A股代码']) and pd.notna(row['A股简称'])}


if __name__ == '__main__':
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = [pool.submit(shanghai, '1'), pool.submit(shanghai, '8'), pool.submit(shenzhen)]
        names = {}
        for task in tasks:
            names.update(task.result())
    assert names.get('601658.SS') == '邮储银行'
    assert names.get('600941.SS') == '中国移动'
    assert names.get('000001.SZ')
    output = Path(__file__).resolve().parents[1] / 'tradingagents/desktop/data/a-share-names.json'
    output.write_text(json.dumps({'as_of': datetime.now().astimezone().isoformat(timespec='seconds'),
                                 'sources': [SSE, SZSE], 'names': dict(sorted(names.items()))},
                                ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f'Bundled {len(names)} exchange-verified A-share names')
