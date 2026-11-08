
from tests.test_desktop_reports import DECISION, sample_report
from tradingagents.desktop.library import ReportLibrary, discover_report_roots
from tradingagents.desktop.store import DesktopStore


def project_at(root):
    root.mkdir(parents=True)
    (root / 'pyproject.toml').write_text('[project]\nname="tradingagents"', encoding='utf-8')
    (root / 'tradingagents').mkdir()
    return root


def test_finder_launch_discovers_project_reports_and_default_engine_directory(tmp_path):
    project = project_at(tmp_path / 'project')
    binary = project / 'dist/StockSeek.app/Contents/MacOS/StockSeek'
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    roots = discover_report_roots(tmp_path / 'engine', cwd=unrelated, executable=binary,
                                  source_file=tmp_path / 'frozen/package/app.py')
    assert project / 'artifacts' in roots
    assert project / 'reports' in roots
    assert tmp_path / 'engine' in roots
    assert unrelated / 'artifacts' not in roots


def test_startup_loads_existing_reports_and_rescans_only_changed_files(tmp_path, monkeypatch):
    source = sample_report(tmp_path / 'reports/one')
    store = DesktopStore(tmp_path / 'workspace')
    library = ReportLibrary(store, [source.parent], settle_seconds=0)
    assert library.scan_once()['added'] == 1
    report = store.list_reports()[0]
    store.save_decision(report['id'], 'observe', '保留个人判断')
    revision = library.status()['revision']
    original_read = store._read
    monkeypatch.setattr(store, '_read', lambda *_: (_ for _ in ()).throw(AssertionError('unchanged report was reread')))
    assert library.scan_once()['added'] == 0
    assert library.status()['revision'] == revision
    monkeypatch.setattr(store, '_read', original_read)
    (source / '5_portfolio/decision.md').write_text(DECISION.replace('Underweight', 'Hold'), encoding='utf-8')
    assert library.scan_once()['added'] == 1
    assert len(store.list_reports()) == 2
    assert library.status()['revision'] > revision
    assert store.history()[0]['text'] == '保留个人判断'


def test_selected_report_directory_is_remembered_after_moving_application(tmp_path):
    source = sample_report(tmp_path / 'other-project/report')
    store = DesktopStore(tmp_path / 'workspace')
    first = ReportLibrary(store, [], settle_seconds=0)
    first.add_directory(source.parent)
    assert first.scan_once()['added'] == 1
    second = ReportLibrary(store, [], settle_seconds=0)
    assert str(source.parent.resolve()) in second.status()['directories']
    (source / '5_portfolio/decision.md').write_text(DECISION.replace('Underweight', 'Buy'), encoding='utf-8')
    assert second.scan_once()['added'] == 1


def test_discovery_never_publishes_its_own_incomplete_or_cancelled_runs(tmp_path):
    store = DesktopStore(tmp_path / 'workspace')
    sample_report(store.directory / 'runs/cancelled')
    good = sample_report(tmp_path / 'reports/good')
    library = ReportLibrary(store, [tmp_path], settle_seconds=0)
    assert library.scan_once()['added'] == 1
    assert store.list_reports()[0]['id'] == store.import_directory(good)['ids'][0]


def test_failed_and_recently_modified_reports_do_not_break_discovery(tmp_path):
    good = sample_report(tmp_path / 'reports/good')
    bad = tmp_path / 'reports/bad'
    bad.mkdir()
    (bad / 'complete_report.md').write_bytes(b'\xff')
    store = DesktopStore(tmp_path / 'workspace')
    library = ReportLibrary(store, [good.parent], settle_seconds=0)
    result = library.scan_once()
    assert result['added'] == 1
    assert result['errors']
    assert library.status()['errors']
    recent = sample_report(tmp_path / 'recent')
    settling = ReportLibrary(DesktopStore(tmp_path / 'second'), [recent], settle_seconds=60)
    assert settling.scan_once()['added'] == 0


def test_discovery_lifecycle_stops_without_leaving_a_process_or_slow_shutdown(tmp_path):
    library = ReportLibrary(DesktopStore(tmp_path / 'workspace'), [], interval=60)
    library.start()
    library.close()
    assert not library._thread.is_alive()


def test_bridge_exposes_automatically_discovered_reports_without_import_action(tmp_path):
    from tradingagents.desktop.api import DesktopAPI

    source = sample_report(tmp_path / 'reports/one')
    store = DesktopStore(tmp_path / 'workspace')
    library = ReportLibrary(store, [source.parent], settle_seconds=0)
    library.scan_once()
    api = DesktopAPI(store, library=library)
    state = api.bootstrap()
    assert state['ok']
    assert len(state['data']['reports']) == 1
    assert state['data']['library']['status'] == 'watching'
    assert api.library_status()['data']['revision'] == 1


def test_bootstrap_does_not_acknowledge_a_revision_newer_than_its_report_list(tmp_path, monkeypatch):
    from tradingagents.desktop.api import DesktopAPI

    source = sample_report(tmp_path / 'reports/one')
    store = DesktopStore(tmp_path / 'workspace')
    library = ReportLibrary(store, [source.parent], settle_seconds=0)
    original = store.pending_reports

    def finish_scan_between_reads():
        items = original()
        library.scan_once()
        return items

    monkeypatch.setattr(store, 'pending_reports', finish_scan_between_reads)
    result = DesktopAPI(store, library=library).bootstrap()['data']
    assert result['reports'] or result['library']['revision'] < library.status()['revision']


def test_invalid_json_shapes_are_isolated_from_later_valid_reports(tmp_path):
    bad = sample_report(tmp_path / 'reports/a-bad')
    (bad / 'run_metadata.json').write_text('[]', encoding='utf-8')
    sample_report(tmp_path / 'reports/z-good')
    store = DesktopStore(tmp_path / 'workspace')
    library = ReportLibrary(store, [bad.parent], settle_seconds=0)
    result = library.scan_once()
    assert result['added'] == 1
    assert len(result['errors']) == 1
    assert library.scan_once()['added'] == 0
