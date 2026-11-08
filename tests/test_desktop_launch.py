from importlib.resources import files

from tradingagents.desktop.app import parse_args


def test_desktop_assets_are_bundled_and_not_demo():
    assets = files('tradingagents.desktop') / 'static'
    assert (assets / 'index.html').is_file()
    assert (assets / 'app.css').is_file()
    assert (assets / 'app.js').is_file()
    html = (assets / 'index.html').read_text(encoding='utf-8')
    assert 'app.js' in html
    assert 'data-page="today"' in html
    assert 'data-page="holdings"' in html
    assert '示例数据' not in html


def test_launch_explicit_data_directory_and_import(tmp_path):
    args = parse_args(['--data-dir', str(tmp_path), '--import-reports', str(tmp_path)])
    assert args.data_dir == tmp_path
    assert args.import_reports == tmp_path


def test_second_window_cannot_take_over_active_workspace(tmp_path):
    import pytest

    from tradingagents.desktop.instance import WorkspaceLock

    with WorkspaceLock(tmp_path), pytest.raises(ValueError, match='已打开'), WorkspaceLock(tmp_path):
        pass
    with WorkspaceLock(tmp_path):
        pass


def test_runtime_check_rejects_missing_bundled_assets(monkeypatch, tmp_path, capsys):
    import sys
    from types import SimpleNamespace

    import pytest

    from tradingagents.desktop import worker

    monkeypatch.delenv('TRADINGAGENTS_DESKTOP_PARENT_PID', raising=False)
    monkeypatch.setattr(sys, 'argv', ['worker', '--check-runtime'])
    monkeypatch.setitem(sys.modules, 'tradingagents.graph.trading_graph',
                        SimpleNamespace(TradingAgentsGraph=object))
    monkeypatch.setattr('importlib.resources.files', lambda _: tmp_path)
    assets = tmp_path / 'static'
    assets.mkdir()
    for name in ('index.html', 'app.js', 'app.css', 'stockseek-mark.png'):
        (assets / name).write_bytes(b'fixture')
    worker.main()
    assert 'runtime-ready' in capsys.readouterr().out
    (assets / 'app.js').unlink()
    with pytest.raises(SystemExit) as error:
        worker.main()
    assert error.value.code == 1
    assert 'FileNotFoundError' in capsys.readouterr().out
