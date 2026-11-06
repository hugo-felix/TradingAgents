"""SQLite-owned immutable report snapshots and explicit personal records."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from time import time
from uuid import uuid4

from tradingagents.desktop.instruments import canonical_symbol, stock_identity
from tradingagents.desktop.reports import SECTION_FILES, STATE_FIELDS, build_document


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def valid_ticker(value: str) -> str:
    value = canonical_symbol(value)
    if not re.fullmatch(r'[A-Z0-9^][A-Z0-9.^=_-]{0,39}', value):
        raise ValueError('请输入有效股票代码，例如 600941.SS 或 AAPL。')
    return value


class DesktopStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / 'workspace.sqlite3'
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY, ticker TEXT NOT NULL, date TEXT NOT NULL,
                    rating TEXT NOT NULL, imported_at TEXT NOT NULL, document TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS report_ticker_date ON reports(ticker, date);
                CREATE INDEX IF NOT EXISTS report_canonical_date ON reports(canonical_ticker(ticker), date);
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY, ticker TEXT NOT NULL, report_id TEXT,
                    kind TEXT NOT NULL, action TEXT NOT NULL, text TEXT NOT NULL, time TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS holdings (
                    ticker TEXT PRIMARY KEY, quantity INTEGER NOT NULL, cost REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS report_sources (path TEXT PRIMARY KEY, signature TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        db.create_function('canonical_ticker', 1, canonical_symbol, deterministic=True)
        db.create_function('stock_name', 1, lambda ticker: stock_identity(ticker)['name'], deterministic=True)
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_directory(self, directory: Path, *, only_changed=False, exclude=(), settle_seconds=0) -> dict:
        root = Path(directory).expanduser().resolve()
        if not root.is_dir():
            raise ValueError('请选择包含 complete_report.md 的报告目录。')
        result = {'added': 0, 'skipped': 0, 'errors': [], 'ids': []}
        excluded = tuple(Path(path).resolve() for path in exclude)
        for path in sorted(root.rglob('complete_report.md')):
            # Resolve every input below the chosen root. No symlinks, including
            # links to section files which might otherwise disclose local files.
            if path.is_symlink() or any((root / p).is_symlink() for p in path.relative_to(root).parents):
                continue
            if any(path.resolve().is_relative_to(folder) for folder in excluded):
                continue
            try:
                signature, latest_change = self._source_signature(path)
                if only_changed:
                    if latest_change > time() - settle_seconds:
                        continue
                    with self.connect() as db:
                        cached = db.execute('SELECT signature FROM report_sources WHERE path=?', (str(path),)).fetchone()
                    if cached and cached['signature'] == signature:
                        result['skipped'] += 1
                        continue
                content = self._read(path, root)
                metadata_path = path.parent / 'run_metadata.json'
                metadata = json.loads(self._read(metadata_path, root)) if metadata_path.exists() else {}
                if not isinstance(metadata, dict):
                    raise ValueError('Report metadata must be a JSON object')
                ticker = metadata.get('ticker') or self._header(content, 'Trading Analysis Report')
                date = metadata.get('analysis_date') or self._header(content, 'Analysis date')
                ticker = valid_ticker(ticker or path.parent.name)
                date = str(date or '日期未知')
                texts = {}
                for key, _title, filename in SECTION_FILES:
                    section_path = path.parent / filename
                    if section_path.exists():
                        texts[key] = self._read(section_path, root)
                state = {}
                state_paths = list((path.parent / 'logs').glob('*/TradingAgentsStrategy_logs/full_states_log_*.json'))
                if len(state_paths) == 1:
                    state = json.loads(self._read(state_paths[0], root))
                if not isinstance(state, dict):
                    raise ValueError('Report state must be a JSON object')
                validation = state.get('report_validation') or {}
                if not isinstance(validation, dict) or any(not isinstance(entry, dict) for entry in validation.values()):
                    raise ValueError('Report validation must contain JSON objects')
                if self._header(content, 'Rating') == 'REVIEW' or metadata.get('rating') == 'REVIEW':
                    state['final_rating'] = 'REVIEW'
                header_rating = metadata.get('rating') or self._header(content, 'Rating')
                from tradingagents.report_language import RATING_ZH
                header_rating = {value: key for key, value in RATING_ZH.items()}.get(header_rating, header_rating)
                if header_rating in {'Buy', 'Overweight', 'Hold', 'Underweight', 'Sell', 'REVIEW'}:
                    state.setdefault('final_rating', header_rating)
                for key, field in STATE_FIELDS.items():
                    if key not in texts and isinstance(state.get(field), str):
                        texts[key] = state[field]
                if not texts:
                    # A standalone consolidated report is still readable. Recover
                    # its final section only when the exact writer heading exists.
                    split = re.split(r'^## V\. (?:Portfolio Manager Decision|最终配置决策)\s*$', content, flags=re.M)
                    if len(split) == 2:
                        texts['decision'] = re.sub(r'^\s*### (?:Portfolio Manager|配置经理)\s*', '', split[1]).strip()
                    texts['complete'] = content
                document = build_document(ticker, date, texts, state)
                document['original_markdown'] = content
                fingerprint = hashlib.sha256(json.dumps(
                    {'document': document, 'run_id': metadata.get('run_id')}, sort_keys=True, ensure_ascii=False,
                ).encode()).hexdigest()[:32]
                document['id'] = fingerprint
                document['imported_at'] = now()
                if self._source_signature(path)[0] != signature:
                    raise ValueError('Report files are still changing; retry after the writer finishes')
                with self.connect() as db:
                    added = db.execute('INSERT OR IGNORE INTO reports VALUES (?, ?, ?, ?, ?, ?)', (
                        fingerprint, ticker, date, document['rating'], document['imported_at'],
                        json.dumps(document, ensure_ascii=False),
                    )).rowcount
                    db.execute('INSERT OR REPLACE INTO report_sources VALUES (?, ?)', (str(path), signature))
                result['added' if added else 'skipped'] += 1
                result['ids'].append(fingerprint)
            except (ValueError, OSError, TypeError) as exc:
                result['errors'].append({'file': path.relative_to(root).as_posix(), 'error': type(exc).__name__})
        return result

    @staticmethod
    def _source_signature(report_path: Path) -> tuple[str, float]:
        directory = report_path.parent
        paths = [report_path, directory / 'run_metadata.json']
        paths.extend(directory / filename for _, _, filename in SECTION_FILES)
        paths.extend(sorted((directory / 'logs').glob('*/TradingAgentsStrategy_logs/full_states_log_*.json')))
        entries, latest_change = [], 0.0
        for path in paths:
            if path.exists():
                stat = path.stat()
                entries.append((str(path.relative_to(directory)), stat.st_size, stat.st_mtime_ns))
                latest_change = max(latest_change, stat.st_mtime)
        return hashlib.sha256(json.dumps(entries).encode()).hexdigest(), latest_change

    @staticmethod
    def _read(path: Path, root: Path) -> str:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('报告路径超出所选目录。')
        if path.stat().st_size > 30_000_000:
            raise ValueError('报告文件超过 30MB。')
        return path.read_text(encoding='utf-8')

    @staticmethod
    def _header(text: str, name: str) -> str:
        aliases = {'Trading Analysis Report': '股票研究报告', 'Analysis date': '分析日期', 'Rating': '评级'}
        labels = re.escape(name) + ('|' + re.escape(aliases[name]) if name in aliases else '')
        match = re.search(rf'^(?:#\s+|-\s+)(?:{labels})[:：]\s*(.+)$', text, re.M)
        return match[1].strip() if match else ''

    def list_reports(self, query='', rating='', limit=100, offset=0) -> list[dict]:
        query = canonical_symbol(query)
        with self.connect() as db:
            rows = db.execute(
                'SELECT id,canonical_ticker(ticker) AS ticker,date,rating,imported_at, '
                'COALESCE(NULLIF(stock_name(ticker),\'\'),json_extract(document,\'$.name\')) AS name, '
                'json_extract(document,\'$.summary.conclusion.text\') AS conclusion FROM reports '
                'WHERE (canonical_ticker(ticker) LIKE ? OR stock_name(ticker) LIKE ? OR json_extract(document,\'$.name\') LIKE ?) AND (? = \'\' OR rating = ?) '
                'ORDER BY date DESC, rowid DESC LIMIT ? OFFSET ?',
                (f'%{str(query)[:80]}%', f'%{str(query)[:80]}%', f'%{str(query)[:80]}%', rating, rating,
                 min(max(int(limit), 1), 200), max(int(offset), 0)),
            ).fetchall()
        from tradingagents.report_language import reading_text
        return [{**dict(row), 'display_conclusion': reading_text(row['conclusion'] or '')} for row in rows]

    def get_report(self, report_id: str) -> dict:
        with self.connect() as db:
            row = db.execute('SELECT document, rowid FROM reports WHERE id=?', (report_id,)).fetchone()
            if row is None:
                raise KeyError('报告不存在。')
            document = json.loads(row['document'])
            document['ticker'] = canonical_symbol(document['ticker'])
            document['name'] = stock_identity(document['ticker'])['name'] or document.get('name', document['ticker'])
            previous = db.execute(
                'SELECT id,date,rating,document FROM reports WHERE canonical_ticker(ticker)=? AND '
                '(date < ? OR (date = ? AND rowid < ?)) ORDER BY date DESC,rowid DESC LIMIT 1',
                (document['ticker'], document['date'], document['date'], row['rowid']),
            ).fetchone()
        document['previous'] = {k: previous[k] for k in ('id', 'date', 'rating')} if previous else None
        document['changes'] = []
        if previous:
            old = json.loads(previous['document'])
            if document['rating'] != old['rating']:
                document['changes'].append({'label': '模型评级', 'before': old['rating'], 'after': document['rating']})
            for key, label in (('conclusion', '结论摘录'), ('conditions', '重新评估条件')):
                before = (old['summary'].get(key) or {}).get('text', '')
                after = (document['summary'].get(key) or {}).get('text', '')
                if before != after:
                    document['changes'].append({'label': label, 'before': before or '未提供', 'after': after or '未提供'})
        from tradingagents.desktop.preview import build_preview
        document['preview'] = build_preview(document)
        from tradingagents.desktop.reports import prepare_reading_document
        return prepare_reading_document(document)

    def save_decision(self, report_id: str, action: str, text: str):
        if action not in {'observe', 'review', 'change'}:
            raise ValueError('未知的处理方式。')
        text = str(text).strip()
        if not text or len(text) > 10000:
            raise ValueError('请填写 1–10000 字的判断依据。')
        report = self.get_report(report_id)
        with self.connect() as db:
            db.execute('INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?)', (
                uuid4().hex, report['ticker'], report_id, 'decision', action, text, now(),
            ))
        return {'saved': True}

    def save_holding(self, ticker: str, quantity: int, cost: float, reason: str):
        ticker = valid_ticker(ticker)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise ValueError('数量必须为非负整数。')
        if not math.isfinite(float(cost)) or float(cost) < 0 or not str(reason).strip():
            raise ValueError('请填写有效成本和更新说明。')
        with self.connect() as db:
            matches = db.execute('SELECT ticker,quantity,cost FROM holdings WHERE canonical_ticker(ticker)=?', (ticker,)).fetchall()
            if len(matches) > 1:
                raise ValueError('存在同一股票的重复代码记录，请先核对持仓，避免误合并数量。')
            old = matches[0] if matches else None
            if old and old['quantity'] == quantity and old['cost'] == float(cost):
                return {'saved': False}
            if old and old['ticker'] != ticker:
                db.execute('DELETE FROM holdings WHERE ticker=?', (old['ticker'],))
            db.execute('INSERT OR REPLACE INTO holdings VALUES (?, ?, ?)', (ticker, quantity, float(cost)))
            before = f"{old['quantity']} 股 / {old['cost']}" if old else '未录入'
            db.execute('INSERT INTO records VALUES (?, ?, NULL, ?, ?, ?, ?)', (
                uuid4().hex, ticker, 'holding', 'snapshot',
                f'{before} → {quantity} 股 / {float(cost)} 元。{str(reason).strip()[:10000]}', now(),
            ))
        return {'saved': True}

    def holdings(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('''
                SELECT canonical_ticker(h.ticker) AS ticker, h.quantity, h.cost,
                       stock_name(h.ticker) AS name, r.id AS report_id, r.date AS report_date, r.rating AS report_rating
                FROM holdings h LEFT JOIN reports r ON r.id = (
                    SELECT id FROM reports WHERE canonical_ticker(ticker)=canonical_ticker(h.ticker) ORDER BY date DESC,rowid DESC LIMIT 1
                ) WHERE quantity>0 ORDER BY h.ticker
            ''')]

    def pending_reports(self, limit=100, offset=0):
        """Latest research per ticker without a personal judgment on that version."""
        with self.connect() as db:
            return [dict(row) for row in db.execute('''
                SELECT r.id,canonical_ticker(r.ticker) AS ticker,r.date,r.rating,r.imported_at,
                       COALESCE(NULLIF(stock_name(r.ticker),''),json_extract(r.document,'$.name')) AS name,
                       json_extract(r.document,'$.summary.conclusion.text') AS conclusion,
                       EXISTS(SELECT 1 FROM reports old WHERE canonical_ticker(old.ticker)=canonical_ticker(r.ticker)
                              AND (old.date<r.date OR (old.date=r.date AND old.rowid<r.rowid))) AS has_previous
                FROM reports r WHERE r.id=(
                    SELECT id FROM reports WHERE canonical_ticker(ticker)=canonical_ticker(r.ticker) ORDER BY date DESC,rowid DESC LIMIT 1
                ) AND NOT EXISTS (SELECT 1 FROM records WHERE report_id=r.id AND kind='decision')
                ORDER BY r.date DESC,r.rowid DESC LIMIT ? OFFSET ?
            ''', (min(max(int(limit), 1), 200), max(int(offset), 0)))]

    def history_entries(self, ticker='', kind='', limit=100, offset=0):
        """Page research versions and personal records together, independently of lists."""
        with self.connect() as db:
            return [dict(row) for row in db.execute('''
                SELECT * FROM (
                    SELECT id,ticker,id AS report_id,'research' AS kind,'' AS action,
                           '' AS text,date AS time,rating,COALESCE(NULLIF(stock_name(ticker),''),json_extract(document,'$.name')) AS name,
                           imported_at AS order_time FROM reports
                    UNION ALL
                    SELECT id,ticker,report_id,kind,action,text,time,'' AS rating,
                           stock_name(ticker) AS name,time AS order_time FROM records
                ) WHERE (?='' OR canonical_ticker(ticker)=? OR name LIKE ?) AND (?='' OR kind=?)
                ORDER BY time DESC,order_time DESC,id DESC LIMIT ? OFFSET ?
            ''', (ticker, canonical_symbol(ticker), f'%{ticker}%', kind, kind, min(max(int(limit), 1), 200), max(int(offset), 0)))]

    def history(self, ticker='', kind='', limit=100, offset=0):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT *,stock_name(ticker) AS name FROM records WHERE (?=\'\' OR canonical_ticker(ticker)=?) AND (?=\'\' OR kind=?) '
                'ORDER BY rowid DESC LIMIT ? OFFSET ?',
                (ticker, canonical_symbol(ticker), kind, kind, min(max(int(limit), 1), 200), max(int(offset), 0)),
            )]

    def setting(self, key: str, value=None):
        with self.connect() as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', (key, json.dumps(value)))
                return value
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row else None
