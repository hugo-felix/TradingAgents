import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.book_jobs import BookTextJobs
from tradingagents.desktop.book_text import BookTextLibrary
from tradingagents.desktop.books import BookRegistry
from tradingagents.desktop.store import DesktopStore


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / 'book.epub'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml',
                         '<container><rootfile full-path="book.opf"/></container>')
        archive.writestr('book.opf', '<package><manifest><item id="c" href="chapter.xhtml" '
                         'media-type="application/xhtml+xml"/></manifest>'
                         '<spine><itemref idref="c"/></spine></package>')
        archive.writestr('chapter.xhtml', '<html><body>Offline source text</body></html>')
    store = DesktopStore(tmp_path / 'workspace')
    metadata = {'title': 'Synthetic', 'author': 'Test', 'translator': 'None',
                'language': 'English', 'edition': 'Test edition', 'source': 'Synthetic',
                'usage_scope': 'Offline tests'}
    book = BookRegistry(store).register(path, metadata)['book']['book_id']
    jobs = BookTextJobs(store)
    yield store, book, jobs
    jobs.close()
    if jobs.thread:
        jobs.thread.join(timeout=5)
        assert not jobs.thread.is_alive()


def wait(jobs):
    jobs.thread.join(timeout=5)
    assert not jobs.thread.is_alive()


def paused(jobs, monkeypatch, *, after_extract=False):
    entered, release = threading.Event(), threading.Event()
    parse = jobs.library.parse

    def parse_paused(*args, **kwargs):
        if after_extract:
            result = parse(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result if after_extract else parse(*args, **kwargs)

    monkeypatch.setattr(jobs.library, 'parse', parse_paused)
    return entered, release


def test_success_and_dedup_keep_original_timestamp(setup):
    store, book, jobs = setup
    store.setting('task', {'status': 'running', 'id': 'independent-analysis'})
    first = jobs.start(book)
    wait(jobs)
    result = jobs.status(book)
    assert first['id'] == result['id']
    assert result['status'] == 'completed'
    assert result['completed_units'] == 1
    original = jobs.library.get(result['parse_id'])
    assert original['units'][0]['text'] == 'Offline source text'
    jobs.start(book)
    wait(jobs)
    assert jobs.library.get(result['parse_id']) == original
    assert len(jobs.library.history(book)) == 1
    assert store.setting('task')['id'] == 'independent-analysis'


@pytest.mark.parametrize('after_extract', [False, True])
def test_cancellation_before_publication_never_publishes(setup, monkeypatch, after_extract):
    _, book, jobs = setup
    entered, release = paused(jobs, monkeypatch, after_extract=after_extract)
    try:
        job = jobs.start(book)
        assert entered.wait(5)
        assert jobs.cancel(job['id'])['status'] == 'cancelling'
        assert jobs.library.history(book) == []
    finally:
        release.set()
    wait(jobs)
    assert jobs.status(book)['status'] == 'cancelled'
    assert jobs.status(book)['parse_id'] is None
    assert jobs.library.history(book) == []
    assert jobs.cancel(job['id'])['status'] == 'cancelled'


def test_concurrent_starts_and_stale_cancel(setup, monkeypatch):
    _, book, jobs = setup
    entered, release = paused(jobs, monkeypatch)
    try:
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: jobs.start(book), range(8)))
        assert entered.wait(5)
        assert len({result['id'] for result in results}) == 1
        old = results[0]['id']
        jobs.cancel(old)
    finally:
        release.set()
    wait(jobs)
    jobs.start(book)
    wait(jobs)
    assert jobs.cancel(old)['status'] == 'cancelled'
    assert jobs.status(book)['status'] == 'completed'
    with pytest.raises(ValueError, match='未找到'):
        jobs.cancel('missing')


def test_failed_job_can_retry_and_old_version_survives(setup, monkeypatch):
    _, book, jobs = setup
    jobs.start(book)
    wait(jobs)
    original = jobs.library.history(book)
    with monkeypatch.context() as scoped:
        def fail(*args, **kwargs):
            kwargs['progress'](2)
            raise ValueError('sensitive path or document content')
        scoped.setattr(jobs.library, 'parse', fail)
        jobs.start(book)
        wait(jobs)
    failed = jobs.status(book)
    assert failed['status'] == 'failed'
    assert failed['completed_units'] == 2
    assert 'sensitive' not in failed['error']
    assert failed['parse_id'] is None
    assert jobs.library.history(book) == original
    jobs.start(book)
    wait(jobs)
    assert jobs.status(book)['status'] == 'completed'
    assert jobs.library.history(book) == original


def test_publication_and_success_commit_atomically(setup, monkeypatch):
    _, book, jobs = setup
    save = jobs._save

    def fail_success(job, db):
        if job['status'] == 'completed':
            raise OSError('storage failure')
        return save(job, db)

    monkeypatch.setattr(jobs, '_save', fail_success)
    jobs.start(book)
    wait(jobs)
    assert jobs.status(book)['status'] == 'failed'
    assert jobs.library.history(book) == []


def test_restart_retains_progress_and_marks_interruption(setup):
    store, book, jobs = setup
    with store.connect() as db:
        jobs._save({'id': 'previous', 'book_id': book, 'status': 'running', 'completed_units': 7,
                    'parse_id': None, 'started_at': '2026-11-10T00:00:00+00:00'}, db)
    restored = BookTextJobs(DesktopStore(store.directory))
    status = restored.status(book)
    assert status['status'] == 'interrupted'
    assert status['completed_units'] == 7
    assert restored.library.history(book) == []
    restored.start(book)
    wait(restored)
    assert restored.status(book)['completed_units'] == 1
    restored.close()


def test_close_blocks_publication_and_new_jobs(setup, monkeypatch):
    _, book, jobs = setup
    entered, release = paused(jobs, monkeypatch, after_extract=True)
    try:
        jobs.start(book)
        assert entered.wait(5)
        jobs.close()
        with pytest.raises(ValueError, match='退出'):
            jobs.start(book)
    finally:
        release.set()
    wait(jobs)
    assert jobs.status(book)['status'] == 'cancelled'
    assert jobs.library.history(book) == []


def test_stage_without_publish_and_bridge(setup):
    store, book, jobs = setup
    counts = []
    staged = BookTextLibrary(store).parse(book, persist=False, progress=counts.append)
    assert counts == [0, 1]
    assert staged['units']
    assert jobs.library.history(book) == []
    api = DesktopAPI(store)
    try:
        assert api.book_text_status(book)['data']['status'] == 'idle'
        assert not api.start_book_text('missing')['ok']
        assert api.start_book_text(book)['ok']
        wait(api._book_jobs)
        assert api.book_text_status(book)['data']['status'] == 'completed'
    finally:
        api._book_jobs.close()
