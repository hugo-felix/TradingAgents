"""Native desktop entry point. Importing it never requires a GUI runtime."""

from __future__ import annotations

import argparse
import os
import sys
from importlib.resources import files
from pathlib import Path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='StockSeek · TradingAgents 本地投资研究工作台')
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get(
        'TRADINGAGENTS_DESKTOP_DIR', str(Path.home() / '.tradingagents' / 'desktop'))))
    parser.add_argument('--import-reports', type=Path, help='首次打开时导入此目录下的历史报告')
    parser.add_argument('--env-file', type=Path, help='从指定 .env 文件读取现有服务配置')
    parser.add_argument('--debug', action='store_true', help='开启 WebView 开发工具')
    return parser.parse_args(argv)


def main(argv=None):
    if '--desktop-worker' in sys.argv:
        from tradingagents.desktop.worker import main as worker_main
        sys.argv.remove('--desktop-worker')
        return worker_main()
    args = parse_args(argv)
    try:
        import webview
    except ImportError:
        raise SystemExit('请先安装桌面依赖：pip install ".[desktop,a-shares]"') from None
    from dotenv import load_dotenv
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    elif not getattr(sys, 'frozen', False):
        load_dotenv(Path.cwd() / '.env', override=False)
    from tradingagents.default_config import build_default_config
    from tradingagents.desktop.api import DesktopAPI
    from tradingagents.desktop.instance import WorkspaceLock
    from tradingagents.desktop.library import ReportLibrary, discover_report_roots
    from tradingagents.desktop.store import DesktopStore
    try:
        lock = WorkspaceLock(args.data_dir).__enter__()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    store = DesktopStore(args.data_dir)
    if args.import_reports:
        result = store.import_directory(args.import_reports)
        print(f"已导入 {result['added']} 份报告；重复 {result['skipped']}；失败 {len(result['errors'])}")
    roots = discover_report_roots(Path(build_default_config()['results_dir']))
    if args.import_reports:
        roots.append(args.import_reports)
    library = ReportLibrary(store, roots)
    api = DesktopAPI(store, library=library)
    library.start()
    asset = files('tradingagents.desktop') / 'static' / 'index.html'
    window = webview.create_window(
        'StockSeek', Path(str(asset)).resolve().as_uri(), js_api=api, width=1320, height=900,
        min_size=(760, 600), background_color='#0b1016', text_select=True,
    )
    api._window = window
    window.events.closed += api._tasks.close
    window.events.closed += library.close
    try:
        # Windows must use modern Edge; no silent fallback to the old IE renderer.
        webview.start(gui='edgechromium' if sys.platform == 'win32' else None,
                      debug=args.debug, http_server=False, private_mode=True,
                      storage_path=str(store.directory / 'webview'))
    finally:
        library.close()
        api._tasks.close()
        lock.__exit__()


if __name__ == '__main__':
    main()
