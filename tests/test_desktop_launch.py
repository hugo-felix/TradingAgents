from importlib.resources import files

from tradingagents.desktop.app import parse_args


def test_desktop_assets_are_bundled_and_not_demo():
    assets = files('tradingagents.desktop') / 'static'
    assert (assets / 'index.html').is_file()
    assert (assets / 'app.css').is_file()
    assert (assets / 'app.js').is_file()
    html = (assets / 'index.html').read_text()
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
