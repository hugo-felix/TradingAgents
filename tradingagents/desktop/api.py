"""Small, explicit JS bridge; no arbitrary filesystem or process API."""

from __future__ import annotations

import os
import webbrowser
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from tradingagents.desktop.reports import export_markdown
from tradingagents.desktop.store import DesktopStore
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


class DesktopAPI:
    def __init__(self, store: DesktopStore, library=None):
        self._store = store
        self._library = library
        self._window = None
        self._tasks = TaskManager(store, environment_builder=self._credential_environment)

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
