"""Independent, bounded AkShare qualification-data probe; no screening decisions."""
import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from tradingagents.dataflows.dividend_screen import UNIVERSE, atomic_json

CASES = {
    'control': ('stock_hold_control_cninfo', {'symbol': '全部'}),
    'profile': ('stock_profile_cninfo', {'symbol': '600941'}),
    'company': ('stock_individual_basic_info_xq', {'symbol': 'SH600941'}),
    'industry': ('stock_board_industry_cons_em', {'symbol': '通信服务'}),
}


def worker(case):
    import akshare as ak

    endpoint, kwargs = CASES[case]
    try:
        frame = getattr(ak, endpoint)(**kwargs)
        print(json.dumps({'ok': True, 'version': ak.__version__, 'columns': list(frame.columns),
                          'rows': json.loads(frame.to_json(orient='records', date_format='iso', force_ascii=False))}, ensure_ascii=False))
    except Exception as exc:
        # Exception messages can contain authentication headers; retain only the class.
        print(json.dumps({'ok': False, 'version': ak.__version__, 'error': type(exc).__name__}))


def run(directory):
    def one(job):
        case, direct = job
        env = dict(os.environ, PYTHONUTF8='1')
        if direct:
            env.update(NO_PROXY='*', no_proxy='*')
        result = {'case': case, 'endpoint': CASES[case][0], 'kwargs': CASES[case][1],
                  'direct': direct, 'checked_at': datetime.now().astimezone().isoformat()}
        try:
            proc = subprocess.run([sys.executable, '-m', 'tradingagents.dataflows.dividend_probe', '--worker', case],
                                  capture_output=True, encoding='utf-8', env=env, timeout=45,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)
            packet = json.loads(proc.stdout)
            rows = packet.pop('rows', [])
            result.update(packet, row_count=len(rows))
            if rows:
                atomic_json(directory / f'{case}-{direct}.json', rows)
            if case == 'control':
                covered = {str(r.get('证券代码', '')).zfill(6) for r in rows}
                result['candidate_coverage'] = [c for c, _, _ in UNIVERSE if c in covered]
                result['candidate_rows'] = [r for r in rows if str(r.get('证券代码', '')).zfill(6) in {c for c, _, _ in UNIVERSE}]
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            result.update(ok=False, error=type(exc).__name__)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, [(case, direct) for direct in (False, True) for case in CASES]))
    atomic_json(directory / 'summary.json', results)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('output/akshare-qualification-probe'))
    parser.add_argument('--worker', choices=CASES)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
    else:
        run(args.output)
