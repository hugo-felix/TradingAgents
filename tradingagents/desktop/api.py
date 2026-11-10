"""Small, explicit JS bridge; no arbitrary filesystem or process API."""

from __future__ import annotations

import json
import math
import os
import threading
import webbrowser
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from tradingagents.dataflows.dividend_screen import annual_snapshot
from tradingagents.desktop.dividends import DividendJobs
from tradingagents.desktop.planning import strategy_plan, validate_preferences
from tradingagents.desktop.reports import export_markdown
from tradingagents.desktop.store import DesktopStore, now
from tradingagents.desktop.tasks import TaskManager, validate_request
from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV


def response(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return {'ok': True, 'data': method(*args, **kwargs)}
        except (ValueError, KeyError) as exc:
            return {'ok': False, 'error': str(exc).strip("'")}
        except Exception:
            return {'ok': False, 'error': '操作未完成，请检查配置和本地文件权限后重试。'}
    return wrapped


def validate_dividend_snapshot(data):
    """Reject incomplete snapshots before they can break the desktop renderer."""
    if not isinstance(data, dict) or data.get('schema_version') not in (1, 2):
        raise ValueError('unsupported snapshot')
    if not isinstance(data.get('items'), list):
        raise ValueError('invalid items')
    for field in ('generated_at', 'as_of', 'coverage', 'rules', 'limitations'):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError('missing snapshot metadata')
    generated = datetime.fromisoformat(data['generated_at'])
    if generated.tzinfo is None:
        raise ValueError('missing timestamp timezone')
    date.fromisoformat(data['as_of'])
    if type(data.get('require_qualifications')) is not bool:
        raise ValueError('invalid qualification policy')
    statuses = {'pass', 'pending', 'fail'}
    changes = data.get('changes')
    if changes is not None:
        if not isinstance(changes, dict) or not isinstance(changes.get('items'), list):
            raise ValueError('invalid changes')
        previous_at = changes.get('previous_at')
        if previous_at is not None and (
                not isinstance(previous_at, str)
                or datetime.fromisoformat(previous_at).tzinfo is None):
            raise ValueError('invalid change timestamp')
        for item in changes['items']:
            if (not isinstance(item, dict)
                    or item.get('kind') not in {'entered', 'exited', 'updated'}
                    or not all(isinstance(item.get(k), str) for k in ('code', 'name'))
                    or not isinstance(item.get('reasons'), list)
                    or not all(isinstance(reason, str) for reason in item['reasons'])):
                raise ValueError('invalid change item')
    for row in data['items']:
        if not isinstance(row, dict):
            raise ValueError('invalid candidate')
        for field in ('code', 'name', 'sector'):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError('missing candidate identity')
        if row.get('status') not in statuses or not isinstance(row.get('metrics'), dict):
            raise ValueError('invalid candidate status or metrics')
        for field in ('yield_pct', 'yield_ttm_pct', 'dividend_annual', 'dividend_ttm', 'price', 'market_cap_yi', 'pe_annual', 'volatility_1y_pct',
                      'volatility_3y_pct', 'max_drawdown_1y_pct', 'max_drawdown_3y_pct',
                      'turnover_20d_yi'):
            value = row['metrics'].get(field)
            if value is not None and (
                type(value) not in (int, float) or not math.isfinite(value)
            ):
                raise ValueError('invalid numeric metric')
        if not isinstance(row.get('checks'), list) or not row['checks']:
            raise ValueError('missing checks')
        for check in row['checks']:
            if not isinstance(check, dict) or check.get('status') not in statuses:
                raise ValueError('invalid check')
            if not all(isinstance(check.get(key), str) for key in ('label', 'detail')):
                raise ValueError('invalid check description')
            if type(check.get('required')) is not bool:
                raise ValueError('invalid check requirement')
        sources, errors = row.get('sources', {}), row.get('errors', [])
        if not isinstance(sources, dict) or not all(isinstance(v, str) for v in sources.values()):
            raise ValueError('invalid sources')
        if not isinstance(errors, list) or not all(isinstance(v, str) for v in errors):
            raise ValueError('invalid errors')
        notes = row.get('risk_notes', [])
        if not isinstance(notes, list) or not all(isinstance(v, str) for v in notes):
            raise ValueError('invalid risk notes')

    if data['schema_version'] == 2:
        discovery = data.get('discovery')
        if not isinstance(discovery, dict) or discovery.get('scope') not in ('all_a', 'subset'):
            raise ValueError('missing discovery manifest')
        codes = discovery.get('candidate_codes')
        actual = [row['code'] for row in data['items']]
        if (not isinstance(codes, list)
                or not all(isinstance(code, str) and len(code) == 6 and code.isascii()
                           and code.isdigit() for code in codes + actual)
                or len(set(actual)) != len(actual)
                or sorted(codes) != sorted(actual)):
            raise ValueError('incomplete discovery manifest')
        for key in ('scanned', 'excluded_small_cap', 'unknown_cap', 'evaluated',
                    'risk_complete', 'provider_error_count'):
            if type(discovery.get(key)) is not int or discovery[key] < 0:
                raise ValueError('invalid discovery counts')
        if (discovery['evaluated'] != len(actual)
                or discovery['scanned'] < len(actual) + discovery['excluded_small_cap']
                or discovery['risk_complete'] > len(actual)
                or discovery['provider_error_count'] > len(actual)
                or discovery['unknown_cap'] > discovery['scanned']):
            raise ValueError('inconsistent discovery counts')
        if discovery['scope'] == 'all_a' and (
                discovery['scanned'] < 4000
                or discovery['scanned'] != len(actual) + discovery['excluded_small_cap']):
            raise ValueError('incomplete all-A scan')
        if data['require_qualifications']:
            raise ValueError('obsolete qualification policy')
        roster = discovery.get('roster')
        if roster is not None:
            if not isinstance(roster, dict) or roster.get('status') not in {
                    'confirmed', 'unconfirmed'}:
                raise ValueError('invalid roster status')
            for key in ('expected', 'observed'):
                if type(roster.get(key)) is not int or roster[key] < 0:
                    raise ValueError('invalid roster counts')
            for key in ('missing_quote_codes', 'unlisted_quote_codes'):
                values = roster.get(key)
                if (not isinstance(values, list) or not all(
                        isinstance(code, str) and len(code) == 6 and code.isascii()
                        and code.isdigit() for code in values)
                        or len(values) != len(set(values))):
                    raise ValueError('invalid roster codes')
            if (roster['expected'] + len(roster['unlisted_quote_codes'])
                    != roster['observed'] + len(roster['missing_quote_codes'])):
                raise ValueError('inconsistent roster reconciliation')
            if discovery['scope'] == 'all_a' and not set(
                    roster['missing_quote_codes']).issubset(actual):
                raise ValueError('missing quotes must remain candidates')


class DesktopAPI:
    @response
    def register_book(self, metadata):
        from tradingagents.desktop.books import BookRegistry, validate_metadata
        validate_metadata(metadata)
        if self._window is None:
            raise ValueError('请在桌面窗口中选择书籍文件。')
        import webview
        chosen = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=False,
            file_types=('Books (*.pdf;*.epub)',))
        if not chosen:
            return {'cancelled': True}
        path = Path(chosen if isinstance(chosen, str) else chosen[0])
        return BookRegistry(self._store).register(path, metadata)

    @response
    def list_books(self):
        from tradingagents.desktop.books import BookRegistry
        return BookRegistry(self._store).list_books()

    @response
    def get_book(self, book_id):
        from tradingagents.desktop.books import BookRegistry
        return BookRegistry(self._store).get(book_id)

    @response
    def verify_book(self, book_id):
        from tradingagents.desktop.books import BookRegistry
        return BookRegistry(self._store).verify(book_id)

    @response
    def parse_book_text(self, book_id):
        from tradingagents.desktop.book_text import BookTextLibrary
        document = BookTextLibrary(self._store).parse(book_id)
        return {key: value for key, value in document.items() if key != 'units'}

    @response
    def book_text_history(self, book_id):
        from tradingagents.desktop.book_text import BookTextLibrary
        return BookTextLibrary(self._store).history(book_id)

    @response
    def search_book_text(self, parse_id, query):
        from tradingagents.desktop.book_text import BookTextLibrary
        return BookTextLibrary(self._store).search(parse_id, query)

    @response
    def book_text_citation(self, parse_id, ordinal):
        from tradingagents.desktop.book_text import BookTextLibrary
        return BookTextLibrary(self._store).citation(parse_id, ordinal)

    @response
    def list_strategies(self):
        from tradingagents.desktop.strategies import StrategyRegistry
        return StrategyRegistry(self._store).list_strategies()

    @response
    def strategy_history(self, strategy_id):
        from tradingagents.desktop.strategies import StrategyRegistry
        return StrategyRegistry(self._store).history(strategy_id)

    @response
    def save_strategy(self, definition, strategy_id=None, expected_version=0):
        from tradingagents.desktop.strategies import StrategyRegistry
        return StrategyRegistry(self._store).save(
            definition, strategy_id=strategy_id, expected_version=expected_version)

    @response
    def get_strategy(self, strategy_id, version=None):
        from tradingagents.desktop.strategies import StrategyRegistry
        return StrategyRegistry(self._store).get(strategy_id, version)

    @response
    def confirm_strategy(self, strategy_id, expected_version, confirmed_by):
        from tradingagents.desktop.strategies import StrategyRegistry
        return StrategyRegistry(self._store).confirm(
            strategy_id, expected_version=expected_version, confirmed_by=confirmed_by)

    @response
    def record_strategy_run(self, strategy_id, version, manifest):
        from tradingagents.desktop.strategy_runs import StrategyRunRegistry
        return StrategyRunRegistry(self._store).record(strategy_id, version, manifest)

    @response
    def list_strategy_runs(self, strategy_id, version):
        from tradingagents.desktop.strategy_runs import StrategyRunRegistry
        return StrategyRunRegistry(self._store).list_runs(strategy_id, version)

    @response
    def check_strategy_conditions(self, strategy_id, version, as_of, data):
        from tradingagents.desktop.strategy_checks import StrategyCheckRegistry
        return StrategyCheckRegistry(self._store).check(
            strategy_id, version, as_of=as_of, data=data)

    @response
    def get_strategy_check(self, check_id):
        from tradingagents.desktop.strategy_checks import StrategyCheckRegistry
        return StrategyCheckRegistry(self._store).get(check_id)

    @response
    def strategy_check_history(self, strategy_id, version):
        from tradingagents.desktop.strategy_checks import StrategyCheckRegistry
        return StrategyCheckRegistry(self._store).history(strategy_id, version)

    @response
    def assess_strategy_timing(self, check_id, evidence):
        from tradingagents.desktop.strategy_timing import StrategyTimingRegistry
        return StrategyTimingRegistry(self._store).assess(check_id, evidence)

    @response
    def get_strategy_timing(self, audit_id):
        from tradingagents.desktop.strategy_timing import StrategyTimingRegistry
        return StrategyTimingRegistry(self._store).get(audit_id)

    @response
    def strategy_timing_history(self, check_id):
        from tradingagents.desktop.strategy_timing import StrategyTimingRegistry
        return StrategyTimingRegistry(self._store).history(check_id)

    @response
    def preview_actual_trades(self, csv_text, source_label):
        from tradingagents.desktop.trades import TradeLedger
        return TradeLedger(self._store).preview(csv_text, source_label)

    @response
    def import_actual_trades(self, csv_text, source_label, expected_sha256):
        from tradingagents.desktop.trades import TradeLedger
        return TradeLedger(self._store).import_csv(csv_text, source_label, expected_sha256)

    @response
    def get_trade_import(self, batch_id):
        from tradingagents.desktop.trades import TradeLedger
        return TradeLedger(self._store).get_import(batch_id)

    @response
    def list_actual_trades(self, ticker=None, limit=100, offset=0):
        from tradingagents.desktop.trades import TradeLedger
        return TradeLedger(self._store).list_fills(ticker, limit=limit, offset=offset)

    @response
    def get_actual_trade(self, trade_id):
        from tradingagents.desktop.trades import TradeLedger
        return TradeLedger(self._store).get_fill(trade_id)

    @response
    def record_trade_context(self, trade_id, context, expected_version=0):
        from tradingagents.desktop.trade_contexts import TradeContextRegistry
        return TradeContextRegistry(self._store).record(
            trade_id, context, expected_version=expected_version)

    @response
    def get_trade_context(self, trade_id, version=None):
        from tradingagents.desktop.trade_contexts import TradeContextRegistry
        return TradeContextRegistry(self._store).get(trade_id, version)

    @response
    def trade_context_history(self, trade_id, limit=100, offset=0):
        from tradingagents.desktop.trade_contexts import TradeContextRegistry
        return TradeContextRegistry(self._store).history(trade_id, limit=limit, offset=offset)

    @response
    def record_operation(self, kind, payload, request_id):
        from tradingagents.desktop.operations import OperationLedger
        return OperationLedger(self._store).record(kind, payload, request_id)

    @response
    def get_operation(self, operation_id):
        from tradingagents.desktop.operations import OperationLedger
        return OperationLedger(self._store).get(operation_id)

    @response
    def list_operations(self, kind=None, ticker=None, limit=100, offset=0):
        from tradingagents.desktop.operations import OperationLedger
        return OperationLedger(self._store).list(kind, ticker, limit=limit, offset=offset)

    @response
    def record_account_event(self, kind, payload):
        from tradingagents.desktop.account_events import AccountEventLedger
        return AccountEventLedger(self._store).record(kind, payload)

    @response
    def get_account_event(self, record_id):
        from tradingagents.desktop.account_events import AccountEventLedger
        return AccountEventLedger(self._store).get(record_id)

    @response
    def void_account_event(self, record_id, request_id, reason, recorded_by):
        from tradingagents.desktop.account_events import AccountEventLedger
        return AccountEventLedger(self._store).void(record_id, request_id, reason, recorded_by)

    @response
    def correct_account_event(self, record_id, request_id, reason, recorded_by, kind, payload):
        from tradingagents.desktop.account_events import AccountEventLedger
        return AccountEventLedger(self._store).correct(
            record_id, request_id, reason, recorded_by, kind, payload)

    @response
    def list_account_events(self, broker=None, account_ref=None, kind=None, limit=100, offset=0):
        from tradingagents.desktop.account_events import AccountEventLedger
        return AccountEventLedger(self._store).list(
            broker, account_ref, kind, limit=limit, offset=offset)

    @response
    def record_account_reconciliation(self, statement, request_id, recorded_by):
        from tradingagents.desktop.account_reconciliation import AccountReconciliation
        return AccountReconciliation(self._store).record(statement, request_id, recorded_by)

    @response
    def get_account_reconciliation(self, request_id):
        from tradingagents.desktop.account_reconciliation import AccountReconciliation
        return AccountReconciliation(self._store).get(request_id)

    @response
    def list_account_reconciliations(self, limit=100, offset=0):
        from tradingagents.desktop.account_reconciliation import AccountReconciliation
        return AccountReconciliation(self._store).list(limit=limit, offset=offset)

    @response
    def record_trade_review(self, trade_id, notes, request_id):
        from tradingagents.desktop.reviews import ReviewRegistry
        return ReviewRegistry(self._store).record(trade_id, notes, request_id)

    @response
    def get_trade_review(self, review_id):
        from tradingagents.desktop.reviews import ReviewRegistry
        return ReviewRegistry(self._store).get(review_id)

    @response
    def list_trade_reviews(self, trade_id=None, limit=100, offset=0):
        from tradingagents.desktop.reviews import ReviewRegistry
        return ReviewRegistry(self._store).list(trade_id, limit=limit, offset=offset)

    @response
    def create_review_strategy(self, review_id, definition):
        from tradingagents.desktop.reviews import ReviewRegistry
        return ReviewRegistry(self._store).create_draft(review_id, definition)

    def __init__(self, store: DesktopStore, library=None):
        self._store = store
        self._library = library
        self._window = None
        self._tasks = TaskManager(store, environment_builder=self._credential_environment)
        self._dividends = DividendJobs(store)
        self._watch_lock = threading.RLock()

    @staticmethod
    def _credential_environment(provider):
        env_name = PROVIDER_API_KEY_ENV.get(provider)
        if not env_name:
            return {}
        # An explicitly saved desktop credential takes precedence for this child only.
        try:
            import keyring
            key = keyring.get_password('TradingAgents.Desktop', provider)
        except Exception:
            key = None
        return {env_name: key} if key else {}

    @response
    def bootstrap(self):
        from tradingagents.default_config import build_default_config
        # Capture the revision before list reads. A scan finishing between reads
        # must still cause a subsequent UI refresh, rather than be acknowledged.
        library_state = self._library_state()
        defaults = build_default_config()
        settings = self._store.setting('model') or {
            'provider': defaults['llm_provider'], 'quick_model': defaults['quick_think_llm'],
            'deep_model': defaults['deep_think_llm'], 'backend_url': defaults.get('backend_url') or '',
        }
        return {
            'reports': self._store.list_reports(), 'holdings': self._store.holdings(),
            'records': self._store.history(), 'task': self._tasks.status(), 'settings': settings,
            'pending': self._store.pending_reports(),
            'library': library_state,
            'providers': list(PROVIDER_API_KEY_ENV), 'theme': self._store.setting('theme') or 'dark',
            'data_directory': str(self._store.directory),
        }

    @response
    def dividend_status(self):
        return self._dividends.status()

    @response
    def set_dividend_auto_update(self, enabled):
        return self._dividends.set_auto_update(enabled)

    @response
    def refresh_dividends(self, direct=False, environment_only=False):
        return self._dividends.start(direct, environment_only)

    @response
    def cancel_dividends(self):
        return self._dividends.cancel()

    @response
    def dividend_watchlist(self):
        with self._watch_lock:
            return self._store.setting('dividend_watchlist') or []

    @response
    def save_dividend_watch(self, code, name, note=None):
        if (not isinstance(code, str) or len(code) != 6
                or not code.isascii() or not code.isdigit()):
            raise ValueError('请输入六位A股代码。')
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError('股票名称无效。')
        if note is not None and (not isinstance(note, str) or len(note) > 2000):
            raise ValueError('备注最多2000字。')
        with self._watch_lock:
            rows = self._store.setting('dividend_watchlist') or []
            row = next((r for r in rows if r['code'] == code), None)
            if row is None:
                row = {'code': code, 'name': name.strip(), 'note': '', 'added_at': now()}
                rows.append(row)
            if note is not None:
                row['note'] = note
            return self._store.setting('dividend_watchlist', rows)

    @response
    def remove_dividend_watch(self, code):
        with self._watch_lock:
            rows = self._store.setting('dividend_watchlist') or []
            return self._store.setting('dividend_watchlist', [r for r in rows if r['code'] != code])

    @response
    def dividend_screen(self):
        return self._read_dividend_screen()

    def _read_dividend_screen(self):
        path = self._store.directory / 'dividend-screen.json'
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            validate_dividend_snapshot(data)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError('高股息快照格式无效，请点击“刷新筛选”重新获取。') from exc
        return annual_snapshot(data, self._store.directory / 'dividend-cache',
                               self._store.directory / 'dividend-official-evidence.json')

    @response
    def entry_plan(self):
        preferences = self._store.setting('entry_preferences')
        snapshot = self._read_dividend_screen()
        strategy = strategy_plan(snapshot, preferences, self._store.holdings())
        return {
            'preferences': preferences,
            'as_of': snapshot['as_of'] if snapshot else None,
            'generated_at': snapshot['generated_at'] if snapshot else None,
            **strategy,
        }

    @response
    def save_entry_preferences(self, values):
        return self._store.setting('entry_preferences', validate_preferences(values))

    @response
    def list_reports(self, query='', rating='', offset=0):
        return self._store.list_reports(query=query, rating=rating, offset=offset)

    @response
    def get_report(self, report_id):
        return self._store.get_report(str(report_id))

    @response
    def get_preview(self, report_id):
        document = self._store.get_report(str(report_id))
        return {key: document[key] for key in (
            'id', 'ticker', 'name', 'date', 'rating', 'model_rating', 'preview', 'warnings', 'previous', 'changes',
        )}

    @response
    def resolve_stock(self, ticker):
        from tradingagents.desktop.instruments import stock_identity
        return stock_identity(ticker)

    @response
    def import_reports(self):
        if self._window is None:
            raise ValueError('请在桌面窗口中选择报告目录。')
        import webview
        chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not chosen:
            return {'cancelled': True}
        directory = Path(chosen[0])
        result = self._store.import_directory(directory)
        if self._library:
            self._library.add_directory(directory)
        return result

    def _library_state(self):
        return self._library.status() if self._library else {'status': 'idle', 'revision': 0, 'directories': [], 'errors': []}

    @response
    def library_status(self):
        return self._library_state()

    @response
    def scan_reports(self):
        return self._library.scan_once() if self._library else {'added': 0, 'errors': []}

    @response
    def save_decision(self, report_id, action, text):
        return self._store.save_decision(report_id, action, text)

    @response
    def save_holding(self, ticker, quantity, cost, reason):
        return self._store.save_holding(ticker, quantity, cost, reason)

    @response
    def history(self, ticker='', kind='', offset=0):
        return self._store.history(ticker=ticker, kind=kind, offset=offset)

    @response
    def history_entries(self, ticker='', kind='', offset=0):
        return self._store.history_entries(ticker=ticker, kind=kind, offset=offset)

    @response
    def pending_reports(self, offset=0):
        return self._store.pending_reports(offset=offset)

    @response
    def start_analysis(self, request):
        return self._tasks.start(request)

    @response
    def task_status(self):
        return self._tasks.status()

    @response
    def cancel_analysis(self):
        return self._tasks.cancel()

    @response
    def save_settings(self, settings):
        if not isinstance(settings, dict) or set(settings) - {'provider', 'quick_model', 'deep_model', 'backend_url'}:
            raise ValueError('只允许保存模型名称、服务商和服务地址。')
        from datetime import date
        checked = validate_request({**settings, 'ticker': 'AAPL', 'date': date.today().isoformat()})
        return self._store.setting('model', {k: v for k, v in checked.items() if k not in {'ticker', 'date'}})

    @response
    def credential_status(self, provider):
        if provider not in PROVIDER_API_KEY_ENV:
            raise ValueError('未知服务商。')
        env_name = PROVIDER_API_KEY_ENV[provider]
        saved = self._credential_environment(provider)
        return {'configured': bool(saved or (env_name and os.environ.get(env_name))), 'required': env_name is not None}

    @response
    def save_credential(self, provider, key):
        if provider not in PROVIDER_API_KEY_ENV or not PROVIDER_API_KEY_ENV[provider]:
            raise ValueError('此服务商不使用 API Key。')
        if not isinstance(key, str) or not key.strip() or len(key) > 4096:
            raise ValueError('请输入有效 API Key。')
        import keyring
        keyring.set_password('TradingAgents.Desktop', provider, key.strip())
        return {'saved': True}

    @response
    def set_theme(self, theme):
        if theme not in {'dark', 'light'}:
            raise ValueError('未知主题。')
        return self._store.setting('theme', theme)

    @response
    def export_report(self, report_id):
        document = self._store.get_report(report_id)
        if self._window is None:
            raise ValueError('请在桌面窗口中选择导出位置。')
        import webview
        chosen = self._window.create_file_dialog(
            webview.FileDialog.SAVE, save_filename=f"{document['ticker']}-{document['date']}.md",
            file_types=('Markdown (*.md)',),
        )
        if not chosen:
            return {'cancelled': True}
        destination = Path(chosen if isinstance(chosen, str) else chosen[0])
        text = export_markdown(document)
        destination.write_text(text, encoding='utf-8')
        return {'saved': True}

    @response
    def open_external(self, url):
        parsed = urlsplit(str(url))
        if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('仅允许打开公开 HTTP(S) 链接。')
        webbrowser.open(str(url))
        return {'opened': True}
